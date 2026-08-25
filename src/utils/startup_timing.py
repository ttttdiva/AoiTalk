"""軽量な起動計測ユーティリティ。

起動経路の計測は診断情報であり、計測用のコンソール・ファイル I/O が
アプリケーション起動を妨げてはいけません。このモジュールは常時有効で、
出力に失敗した場合は黙って計測を無効化したまま呼び出し元へ戻ります。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping


def _default_utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PhaseToken:
    """A token returned by :meth:`StartupTimer.start_phase`."""

    phase: str
    started_at: float


class StartupTimer:
    """Thread-safe process-relative startup timer.

    ``monotonic`` and ``utc_now`` are injectable so unit tests can use a fake
    clock without waiting. ``log_path`` may point to a concrete ``.jsonl``
    file, or to a directory in which a run-specific file is created.
    """

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] | None = None,
        utc_now: Callable[[], datetime] | None = None,
        log_path: str | os.PathLike[str] | None = None,
        run_id: str | None = None,
        emit_console: bool = True,
    ) -> None:
        self._monotonic = monotonic or time.monotonic
        self._utc_now = utc_now or _default_utc_now
        self.monotonic_origin = self._safe_monotonic()
        # Keep the id opaque and independent from filesystem/user data.
        if run_id:
            self.run_id = str(run_id)
        else:
            try:
                self.run_id = uuid.uuid4().hex
            except Exception:
                # UUID generation is not expected to fail, but diagnostics
                # must remain fail-open even under a faulty test/platform
                # provider.  The fallback contains no user or secret data.
                self.run_id = f"startup-{id(self):x}"
        self.emit_console = emit_console
        self._lock = threading.RLock()
        self._log_path = self._resolve_log_path(log_path)

    @property
    def origin(self) -> float:
        """Compatibility alias for the process monotonic origin."""

        return self.monotonic_origin

    @property
    def process_monotonic_origin(self) -> float:
        """Explicit name for callers that need the process origin."""

        return self.monotonic_origin

    @property
    def log_path(self) -> Path:
        return self._log_path

    def _safe_monotonic(self) -> float:
        try:
            return float(self._monotonic())
        except Exception:
            # The real monotonic clock should never fail. A fallback keeps a
            # faulty test clock or platform implementation fail-open as well.
            try:
                return float(time.monotonic())
            except Exception:
                return 0.0

    def _resolve_log_path(
        self, log_path: str | os.PathLike[str] | None
    ) -> Path:
        try:
            configured = log_path
            if configured is None:
                configured = (
                    os.getenv("AOITALK_STARTUP_TIMING_PATH")
                    or os.getenv("AOITALK_STARTUP_TIMING_LOG")
                    or os.getenv("STARTUP_TIMING_PATH")
                )

            if configured:
                path = Path(configured).expanduser()
                # A .jsonl value is treated as an explicit file. Other values
                # are directories, which retain the per-run filename guarantee.
                if path.suffix.lower() == ".jsonl":
                    return path
                return path / f"startup_timing_{self.run_id}.jsonl"
        except Exception:
            # A malformed path provider (including a broken ``__fspath__`` or
            # ``expanduser`` implementation) must never abort application
            # import.  Fall through to the process-local default.
            pass

        return Path("logs") / "startup" / f"startup_timing_{self.run_id}.jsonl"

    def start_phase(self, phase: str) -> PhaseToken:
        """Start a phase and return a token for :meth:`finish_phase`."""
        started_at = self._safe_monotonic()
        token = PhaseToken(str(phase), started_at)
        # Emit an explicit start event so a hanging phase remains visible in
        # the JSONL stream.  The token is returned regardless of any logging
        # failure (``record`` is fail-open).
        self.record(
            token.phase,
            duration_ms=0.0,
            status="started",
            elapsed_ms=max(0.0, (started_at - self.monotonic_origin) * 1000.0),
            event="start",
        )
        return token

    # Short aliases make instrumentation call sites readable and preserve a
    # small API surface for callers that prefer start/end terminology.
    start = start_phase

    def finish_phase(
        self,
        token: PhaseToken,
        *,
        status: str = "ok",
    ) -> Mapping[str, object]:
        """Finish a phase and emit its timing record."""

        ended_at = self._safe_monotonic()
        duration_ms = max(0.0, (ended_at - token.started_at) * 1000.0)
        return self.record(
            token.phase,
            duration_ms=duration_ms,
            status=status,
            elapsed_ms=max(0.0, (ended_at - self.monotonic_origin) * 1000.0),
            event="finish",
        )

    finish = finish_phase
    end = finish_phase

    @contextmanager
    def phase(self, phase: str, *, status: str = "ok") -> Iterator[None]:
        """Measure a phase, recording ``status=error`` before re-raising."""

        token = self.start_phase(phase)
        try:
            yield
        except BaseException:
            # ``finish_phase`` is deliberately fail-open; the original
            # exception must always reach the existing startup error handling.
            self.finish_phase(token, status="error")
            raise
        else:
            self.finish_phase(token, status=status)

    measure = phase
    timed = phase

    def mark(self, phase: str, *, status: str = "ok") -> Mapping[str, object]:
        """Emit an instantaneous phase marker."""

        return self.record(phase, duration_ms=0.0, status=status, event="mark")

    def record(
        self,
        phase: str,
        *,
        duration_ms: float = 0.0,
        status: str = "ok",
        elapsed_ms: float | None = None,
        event: str = "mark",
    ) -> Mapping[str, object]:
        """Emit one JSON record to console and the per-run JSONL file.

        Both destinations receive the exact same JSON line. All failures in
        serialization, printing, directory creation, or writing are swallowed
        so that observability cannot make startup fail.
        """

        now = self._safe_monotonic()
        try:
            timestamp = self._utc_now()
        except Exception:
            timestamp = _default_utc_now()
        if not isinstance(timestamp, datetime):
            timestamp = _default_utc_now()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        try:
            record: dict[str, object] = {
                "timestamp": timestamp.astimezone(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "run_id": self.run_id,
                "phase": str(phase),
                "duration_ms": round(max(0.0, float(duration_ms)), 3),
                "elapsed_ms": round(
                    max(
                        0.0,
                        float(elapsed_ms)
                        if elapsed_ms is not None
                        else (now - self.monotonic_origin) * 1000.0,
                    ),
                    3,
                ),
                "status": str(status),
                "event": str(event),
            }
            line = json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except Exception:
            return {}

        # Serialize writes from multiple startup threads. Opening per record
        # avoids a leaked descriptor when startup exits unusually early.
        with self._lock:
            if self.emit_console:
                try:
                    print(f"STARTUP_TIMING {line}", flush=True)
                except Exception:
                    pass
            try:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                with self._log_path.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
            except Exception:
                pass
        return record


_GLOBAL_TIMER = StartupTimer()
startup_timer = _GLOBAL_TIMER


def get_startup_timer() -> StartupTimer:
    """Return the process-wide startup timer."""

    return _GLOBAL_TIMER


__all__ = ["PhaseToken", "StartupTimer", "get_startup_timer", "startup_timer"]
