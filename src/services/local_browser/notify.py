"""Owner notifications for the managed local browser, without exposing secrets."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid
from typing import Any, Callable


_OWNER_ENV = "AOITALK_LOCAL_BROWSER_OWNER_USER_ID"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_logger = logging.getLogger(__name__)
_owner_warning_lock = threading.Lock()
_owner_warning_emitted = False


def configured_owner_user_id(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the configured owner UUID, warning once if it is absent or invalid."""
    env = os.environ if environ is None else environ
    raw = env.get(_OWNER_ENV)
    if not isinstance(raw, str) or not _UUID_RE.fullmatch(raw.strip()):
        warn_owner_not_configured_once()
        return None
    try:
        return str(uuid.UUID(raw.strip()))
    except (ValueError, AttributeError):
        warn_owner_not_configured_once()
        return None


def warn_owner_not_configured_once() -> None:
    global _owner_warning_emitted
    with _owner_warning_lock:
        if _owner_warning_emitted:
            return
        _owner_warning_emitted = True
    _logger.warning(
        "local browser owner is not configured (%s)", "owner_not_configured"
    )


class OwnerNotifier:
    """Queue one notification using the repository's existing Node helper."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        repository_root: Path | None = None,
        unix_time: Callable[[], float] = time.time,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self._environ = environ
        self._repository_root = repository_root or Path(__file__).resolve().parents[3]
        self._unix_time = unix_time
        self._runner = runner

    def notify(self, reason: str, title: str, message: str) -> bool:
        """Queue a deduplicated owner notification; failures are intentionally contained."""
        if not isinstance(reason, str) or not _REASON_RE.fullmatch(reason):
            self._warn_failure("notify_failed")
            return False
        user_id = configured_owner_user_id(self._environ)
        if user_id is None:
            return False
        if not isinstance(title, str) or not title.strip() or len(title) > 255:
            self._warn_failure("notify_failed")
            return False
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            self._warn_failure("notify_failed")
            return False
        try:
            hour = datetime.fromtimestamp(self._unix_time(), timezone.utc).strftime(
                "%Y%m%d%H"
            )
            dedupe_key = f"browser:{reason}:{hour}"
            frontend = self._repository_root / "frontend"
            result = self._runner(
                [
                    "node",
                    "scripts/notify-owner.mjs",
                    "--user-id",
                    user_id,
                    "--dedupe-key",
                    dedupe_key,
                    "--title",
                    title,
                    "--message",
                    message,
                ],
                cwd=str(frontend),
                shell=False,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if getattr(result, "returncode", None) != 0:
                self._warn_failure("notify_failed")
                return False
            payload = json.loads(result.stdout)
            if (
                not isinstance(payload, dict)
                or type(payload.get("inserted")) is not bool
            ):
                self._warn_failure("notify_failed")
                return False
            return payload["inserted"]
        except subprocess.TimeoutExpired:
            self._warn_failure("notify_timeout")
            return False
        except Exception:
            self._warn_failure("notify_failed")
            return False

    @staticmethod
    def _warn_failure(code: str) -> None:
        _logger.warning("local browser notification failed (%s)", code)
