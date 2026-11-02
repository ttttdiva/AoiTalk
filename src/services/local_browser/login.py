"""Short-lived Playwright operations for login metadata and owner session seeding."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Iterator

from .broker import managed_browser_endpoint
from .notify import configured_owner_user_id


_AoiTALK_URL = "http://127.0.0.1:3002"
_SESSION_REFRESH_SECONDS = 48 * 60 * 60
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_COOKIE_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*)?$")


class LocalBrowserLogin:
    """Read cookie metadata or seed AoiTalk's session in the dedicated profile."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        repository_root: Path | None = None,
        endpoint_provider: Callable[[], str | None] | None = None,
        unix_time: Callable[[], float] = time.time,
        runner: Callable[..., Any] = subprocess.run,
        playwright_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._environ = environ
        self._repository_root = repository_root or Path(__file__).resolve().parents[3]
        self._endpoint_provider = endpoint_provider or managed_browser_endpoint
        self._unix_time = unix_time
        self._runner = runner
        self._playwright_factory = playwright_factory

    def login_state(self, site_url: str) -> dict[str, bool | int | float | None]:
        """Synchronously connect to the managed browser and read cookie count/expiry metadata."""
        try:
            with self._connected_context() as context:
                cookies = context.cookies([site_url])
            if not isinstance(cookies, (list, tuple)):
                raise ValueError("cookie_metadata_invalid")
            expiries: list[float] = []
            logged_in = False
            now = self._unix_time()
            for cookie in cookies:
                if not isinstance(cookie, dict):
                    continue
                expiry = cookie.get("expires")
                if type(expiry) in (int, float) and math.isfinite(expiry):
                    expiry = float(expiry)
                    if expiry > 0:
                        expiries.append(expiry)
                        if expiry > now:
                            logged_in = True
                    else:
                        # Playwright uses a nonpositive expiry for a session cookie.
                        logged_in = True
            return {
                "logged_in": logged_in,
                "cookie_count": len(cookies),
                "expires_unix": max(expiries) if expiries else None,
            }
        except Exception:
            raise RuntimeError("login_state_failed") from None

    def ensure_owner_session(self) -> str:
        """Refresh the AoiTalk cookie only when missing or within 48 hours of expiry."""
        user_id = configured_owner_user_id(self._environ)
        if user_id is None:
            return "owner_not_configured"
        try:
            state = self.login_state(_AoiTALK_URL)
        except Exception:
            return "login_cookie_set_failed"
        expiry = state.get("expires_unix")
        if (
            state.get("logged_in") is True
            and type(expiry) in (int, float)
            and expiry - self._unix_time() >= _SESSION_REFRESH_SECONDS
        ):
            return "login_not_needed"

        cookie = self._issue_cookie(user_id)
        if cookie is None:
            return "login_issue_failed"
        try:
            with self._connected_context() as context:
                context.add_cookies(
                    [
                        {
                            "name": cookie["name"],
                            "value": cookie["value"],
                            "domain": "127.0.0.1",
                            "path": cookie["path"],
                            "httpOnly": True,
                            "secure": False,
                            "sameSite": "Lax",
                            "expires": cookie["expires"],
                        }
                    ]
                )
            return "login_seeded"
        except Exception:
            return "login_cookie_set_failed"

    def _issue_cookie(self, user_id: str) -> dict[str, Any] | None:
        frontend = self._repository_root / "frontend"
        try:
            result = self._runner(
                [
                    "node",
                    "scripts/issue-local-browser-session.mjs",
                    "--user-id",
                    user_id,
                ],
                cwd=str(frontend),
                shell=False,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if getattr(result, "returncode", None) != 0:
                return None
            payload = json.loads(result.stdout)
            if not isinstance(payload, dict):
                return None
            name = payload.get("name")
            value = payload.get("value")
            path = payload.get("path")
            expires = payload.get("expires")
            if (
                not isinstance(name, str)
                or not _COOKIE_NAME_RE.fullmatch(name)
                or not isinstance(value, str)
                or not value
                or not isinstance(path, str)
                or not _COOKIE_PATH_RE.fullmatch(path)
                or type(expires) not in (int, float)
                or not math.isfinite(expires)
            ):
                return None
            # Keep the issued value only in this short-lived in-memory object.
            return {
                "name": name,
                "value": value,
                "path": path,
                "expires": float(expires),
            }
        except Exception:
            return None

    @contextmanager
    def _connected_context(self) -> Iterator[Any]:
        endpoint = self._endpoint_provider()
        if not isinstance(endpoint, str) or not endpoint:
            raise RuntimeError("browser_unavailable")
        manager = self._new_playwright()
        with manager as playwright:
            browser = playwright.chromium.connect(endpoint)
            try:
                contexts = browser.contexts
                if not contexts:
                    raise RuntimeError("browser_context_unavailable")
                yield contexts[0]
            finally:
                browser.close()

    def _new_playwright(self) -> Any:
        if self._playwright_factory is not None:
            return self._playwright_factory()
        # Keep Playwright out of module import and out of non-login code paths.
        from playwright.sync_api import sync_playwright

        return sync_playwright()
