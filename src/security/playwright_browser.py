"""Fresh, origin-guarded Playwright contexts for internal QA only.

Director intentionally keeps its separate authenticated launcher. No caller
can supply a profile directory, inherited cookie state, executable, or script.
"""

from __future__ import annotations

import inspect
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .browser_scope import (
    BrowserLane,
    BrowserLaneViolation,
    BrowserRunScope,
    _agent_team_role_bound,
)
from ..utils.subprocess_env import build_aoitalk_subprocess_env

RequestGate = Callable[[Any], Awaitable[dict[str, Any] | None]]


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass
class PlaywrightBrowserLease:
    context: Any
    page: Any
    profile_dir: str

    async def close(self) -> None:
        try:
            await _await(self.context.close())
        finally:
            shutil.rmtree(self.profile_dir, ignore_errors=True)


async def launch_scoped_playwright_browser(
    scope: BrowserRunScope,
    playwright: Any,
    *,
    headless: bool = True,
    request_gate: RequestGate | None = None,
) -> PlaywrightBrowserLease:
    """Install network guards before the first externally reachable page."""
    if _agent_team_role_bound():
        raise BrowserLaneViolation("raw browser launch is parent-owned")
    if scope.lane is not BrowserLane.QA:
        raise BrowserLaneViolation("QA launcher requires the QA lane")
    scope._ensure_active()
    profiles = Path(__file__).resolve().parents[2] / ".local" / "browser-profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    profile_dir = tempfile.mkdtemp(prefix=f"{scope.lane_name}-", dir=profiles)
    context = None
    try:
        context = await _await(
            playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=headless,
                env=build_aoitalk_subprocess_env(),
                service_workers="block",
                accept_downloads=scope.lane is BrowserLane.QA,
            )
        )

        async def guard(route: Any, request: Any) -> None:
            try:
                scope.assert_navigation_allowed(str(request.url))
                overrides = await request_gate(request) if request_gate else {}
                if overrides is None:
                    await _await(route.abort())
                    return
                if "url" in overrides:
                    scope.assert_navigation_allowed(str(overrides["url"]))
                await _await(route.continue_(**overrides))
            except Exception:
                # Origin and transport exceptions may contain private URLs.
                # They are never logged; the controller records a safe code.
                try:
                    await _await(route.abort())
                except Exception:
                    pass

        await _await(context.route("**/*", guard))
        page = context.pages[0] if context.pages else await _await(context.new_page())
        return PlaywrightBrowserLease(context, page, profile_dir)
    except BaseException:
        try:
            if context is not None:
                try:
                    await _await(context.close())
                except Exception:
                    pass
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)
        raise
