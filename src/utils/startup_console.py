"""Concise operator-facing startup status output.

Detailed startup timing remains available through :class:`StartupTimer`'s
per-run JSONL file.  This helper is intentionally small: startup orchestration
can report major stages and one final readiness summary without mirroring every
internal phase to stdout.
"""

from __future__ import annotations

import math
import os
import re
import sys
import threading
import time
from typing import Mapping, TextIO

from .startup_timing import StartupTimer, get_startup_timer


_SECRET_VALUE_PATTERN = re.compile(
    r"""(?ix)
        (?P<key>password|passwd|token|secret|api[_-]?key)\b
        \s*[=:]\s*
        (?:
            (?P<quote>[\"']) (?P<quoted>.*?) (?P=quote)
            |
            (?P<unquoted>[^\s,;]+)
        )
    """
)
# URL userinfo may contain both ``@`` and reserved delimiters such as ``/``,
# ``?``, and ``#`` when a malformed or unencoded connection string reaches an
# exception message.  Match greedily through the last ``@`` in the
# whitespace-delimited token so no password suffix can leak.  Over-redacting
# a malformed URL is safer than exposing credentials in operator output.
_URL_CREDENTIAL_PATTERN = re.compile(r"(?i)(://)[^\s]*@")


def safe_startup_reason(error: BaseException | object, *, limit: int = 240) -> str:
    """Return a bounded, credential-redacted reason suitable for the console."""

    try:
        text = " ".join(str(error).split()).strip()
    except Exception:
        text = ""
    if not text:
        text = type(error).__name__
    text = _URL_CREDENTIAL_PATTERN.sub(r"\1<credentials>@", text)
    text = _SECRET_VALUE_PATTERN.sub(
        lambda match: f"{match.group('key')}=<redacted>",
        text,
    )
    return text[: max(1, int(limit))]


class StartupConsole:
    """Fail-open writer for concise startup progress.

    ``stream`` is resolved lazily when omitted so redirected stdout and pytest
    capture continue to work even when the process-wide singleton was imported
    earlier.  The final :meth:`ready` summary is one-shot and safe to call from
    multiple startup paths/threads.
    """

    def __init__(
        self,
        timer: StartupTimer | None = None,
        *,
        stream: TextIO | None = None,
        detail_log_path: str | os.PathLike[str] | None = None,
        log_file: str | os.PathLike[str] | None = None,
        startup_timing_path: str | os.PathLike[str] | None = None,
        timing_log_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.timer = timer or get_startup_timer()
        self._stream = stream
        self._detail_log_path: str | None = None
        self._startup_timing_path: str | None = None
        self._lock = threading.RLock()
        self._ready_emitted = False
        self.configure(
            detail_log_path=detail_log_path if detail_log_path is not None else log_file,
            startup_timing_path=(
                startup_timing_path
                if startup_timing_path is not None
                else timing_log_path
            ),
        )

    @property
    def detail_log_path(self) -> str | None:
        return self._detail_log_path

    @property
    def startup_timing_path(self) -> str:
        if self._startup_timing_path is not None:
            return self._startup_timing_path
        try:
            return str(self.timer.log_path)
        except Exception:
            return "(startup timing path unavailable)"

    @property
    def ready_emitted(self) -> bool:
        return self._ready_emitted

    def reset_ready(self) -> "StartupConsole":
        """Allow a new application run to emit its own readiness summary."""

        with self._lock:
            self._ready_emitted = False
        return self

    def configure(
        self,
        *,
        detail_log_path: str | os.PathLike[str] | None = None,
        log_file: str | os.PathLike[str] | None = None,
        startup_timing_path: str | os.PathLike[str] | None = None,
    ) -> "StartupConsole":
        """Update paths shown by :meth:`ready` and return ``self``.

        ``log_file`` is a compatibility alias for ``detail_log_path``.  A
        ``None`` value leaves an existing path unchanged, making it safe for
        startup stages to configure the app log before the final summary.
        """

        if detail_log_path is None:
            detail_log_path = log_file
        if detail_log_path is not None:
            try:
                self._detail_log_path = str(detail_log_path)
            except Exception:
                pass
        if startup_timing_path is not None:
            try:
                self._startup_timing_path = str(startup_timing_path)
            except Exception:
                pass
        return self

    # Common setter aliases make integration call sites explicit while keeping
    # the output contract in one place.
    def set_log_file(self, path: str | os.PathLike[str] | None) -> "StartupConsole":
        return self.configure(detail_log_path=path)

    def set_log_paths(
        self,
        detail_log_path: str | os.PathLike[str] | None = None,
        startup_timing_path: str | os.PathLike[str] | None = None,
    ) -> "StartupConsole":
        return self.configure(
            detail_log_path=detail_log_path,
            startup_timing_path=startup_timing_path,
        )

    def _write(self, text: str) -> None:
        """Write one line, swallowing broken-pipe/encoding failures."""

        try:
            stream = self._stream if self._stream is not None else sys.stdout
            print(str(text), file=stream, flush=True)
        except Exception:
            # Observability must not make startup fail (e.g. closed console,
            # redirected pipe, or a cp932 stream that cannot encode a symbol).
            pass

    @staticmethod
    def _has_status_prefix(text: str) -> bool:
        stripped = text.lstrip()
        if stripped.startswith(("⚠", "❌", "✅")):
            return True
        if not stripped.startswith("["):
            return False
        # Treat only known operator statuses as an existing status marker.
        # Labels such as ``[Memory]`` should still receive ``[WARN]`` or
        # ``[ERROR]`` when emitted through those helpers.
        token = stripped.split("]", 1)[0][1:].strip().upper()
        return token in {
            "OK",
            "START",
            "BUILD",
            "SKIP",
            "WARN",
            "WARNING",
            "ERROR",
            "FAIL",
            "FAILED",
            "INFO",
            "STARTUP",
        }

    def stage(self, text: str, status: str | None = None) -> None:
        """Print one deliberate high-level startup stage.

        ``text`` is emitted verbatim by default so callers can use established
        labels such as ``[BUILD] Web UI を更新しています...``.  Passing
        ``status`` adds a bracketed status unless the text is already prefixed.
        """

        try:
            rendered = str(text)
            if status and not self._has_status_prefix(rendered):
                rendered = f"[{str(status).upper()}] {rendered}"
        except Exception:
            return
        with self._lock:
            self._write(rendered)

    def ok(self, text: str) -> None:
        """Convenience alias for an ``[OK]`` stage."""

        self.stage(text, status="OK")

    def warning(self, text: str) -> None:
        """Print an actionable warning without a traceback flood."""

        try:
            rendered = str(text)
            if not self._has_status_prefix(rendered):
                rendered = f"[WARN] {rendered}"
        except Exception:
            return
        with self._lock:
            self._write(rendered)

    def error(self, text: str) -> None:
        """Print an actionable startup error."""

        try:
            rendered = str(text)
            if not self._has_status_prefix(rendered):
                rendered = f"[ERROR] {rendered}"
            if self._detail_log_path and "detailed log" not in rendered.lower():
                rendered = f"{rendered} (Detailed log: {self._detail_log_path})"
        except Exception:
            return
        with self._lock:
            self._write(rendered)

    @staticmethod
    def _elapsed_seconds(timer: StartupTimer, event: Mapping[str, object]) -> float:
        try:
            elapsed_ms = float(event.get("elapsed_ms", float("nan")))
            if math.isfinite(elapsed_ms) and elapsed_ms >= 0:
                return elapsed_ms / 1000.0
        except Exception:
            pass
        try:
            origin = float(timer.monotonic_origin)
            elapsed = time.monotonic() - origin
            if math.isfinite(elapsed) and elapsed >= 0:
                return elapsed
        except Exception:
            pass
        return 0.0

    def ready(
        self,
        web_url: str | None = None,
        *,
        detail_log_path: str | os.PathLike[str] | None = None,
        log_file: str | os.PathLike[str] | None = None,
        startup_timing_path: str | os.PathLike[str] | None = None,
    ) -> bool:
        """Emit the final startup summary once.

        Returns ``True`` when this call emitted the summary and ``False`` when
        another caller had already emitted it.  The timing marker is written
        before the human-readable lines, preserving the complete JSONL stream
        even if console output is unavailable.
        """

        with self._lock:
            if self._ready_emitted:
                return False
            self._ready_emitted = True

            try:
                if detail_log_path is not None or log_file is not None or startup_timing_path is not None:
                    self.configure(
                        detail_log_path=detail_log_path,
                        log_file=log_file,
                        startup_timing_path=startup_timing_path,
                    )
            except Exception:
                pass
            try:
                event = self.timer.mark("startup.ready")
            except Exception:
                event = {}
            elapsed = self._elapsed_seconds(self.timer, event)
            try:
                url = str(web_url) if web_url else "(unavailable)"
            except Exception:
                url = "(unavailable)"
            detail_log = self._detail_log_path
            if detail_log is None:
                try:
                    detail_log = os.getenv("AOITALK_APP_LOG_PATH") or os.getenv(
                        "AOITALK_LOG_PATH"
                    )
                except Exception:
                    detail_log = None
            if not detail_log:
                detail_log = "(application log path unavailable)"

            lines = (
                "[Startup] Ready",
                f"Web: {url}",
                f"Elapsed: {elapsed:.1f}s",
                f"Detailed log: {detail_log}",
                f"Startup timing: {self.startup_timing_path}",
            )
            for line in lines:
                self._write(line)
            return True


_GLOBAL_STARTUP_CONSOLE = StartupConsole()
startup_console = _GLOBAL_STARTUP_CONSOLE


def get_startup_console() -> StartupConsole:
    """Return the process-wide operator startup console."""

    return _GLOBAL_STARTUP_CONSOLE


__all__ = [
    "StartupConsole",
    "get_startup_console",
    "safe_startup_reason",
    "startup_console",
]
