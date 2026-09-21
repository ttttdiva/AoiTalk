"""Application logging configuration.

The application keeps a deliberately small amount of output on the operator
console while retaining the normal INFO (and, in debug mode, DEBUG) stream in
the application log file.  Callers can mark a record as *file only* with
``extra=FILE_ONLY_LOG_EXTRA`` instead of relying on brittle message-prefix
matching.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Dict, Optional


# Structured marker used by code that has useful diagnostics for the app log
# but would be noise on the normal operator console.  Keep this public and
# immutable-by-convention so ``logger.info(..., extra=FILE_ONLY_LOG_EXTRA)`` is
# straightforward at call sites.
FILE_ONLY_LOG_ATTR = "aoitalk_file_only"
FILE_ONLY_LOG_EXTRA: Dict[str, bool] = {FILE_ONLY_LOG_ATTR: True}


def _coerce_level(level: int | str) -> int:
    """Resolve a logging level while retaining the old string API."""

    if isinstance(level, int):
        return level
    try:
        return int(getattr(logging, str(level).upper()))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"Unknown logging level: {level!r}") from exc


class FileOnlyLogFilter(logging.Filter):
    """Hide file-only records unless diagnostic console output is enabled.

    Startup internals use a structured marker rather than message-prefix
    matching.  In normal/operator mode the marker keeps those records out of
    the console while the file handler still receives them.  Debug mode is an
    explicit opt-in to the full diagnostic stream, so the same records are
    allowed through there.
    """

    def __init__(self, *, include_file_only: bool = False) -> None:
        super().__init__()
        self.include_file_only = bool(include_file_only)

    def filter(self, record: logging.LogRecord) -> bool:
        if self.include_file_only:
            return True
        try:
            return not bool(getattr(record, FILE_ONLY_LOG_ATTR, False))
        except Exception:
            # Logging must never become the reason application code fails.
            return True


class SpotipyRateLimitFilter(logging.Filter):
    """Filter the known, repetitive Spotipy rate-limit message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return "Your application has reached a rate/request limit" not in record.getMessage()
        except Exception:
            return True


class LoggingConfig:
    """Unified logging setup with independent root/console/file levels.

    ``level`` remains the application/root level for compatibility.  In
    normal mode (``INFO``) the console defaults to ``WARNING`` while the file
    remains at ``INFO``.  A ``DEBUG`` configuration intentionally sends DEBUG
    records to both destinations.  Explicit ``console_level`` and
    ``file_level`` values are available to specialised callers.
    """

    # Default log format
    DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

    # Keep noisy library INFO records available to the file handler in normal
    # mode while the independent console threshold hides them. Debug mode
    # restores DEBUG so opting into diagnostics really does expose their
    # records.
    SUPPRESSED_LOGGERS = [
        "spotipy",
        "urllib3.util.retry",
        "urllib3.connectionpool",
        "urllib3",
        "httpx",
        "httpcore",
        "spotify",
    ]

    FILE_ONLY_LOG_ATTR = FILE_ONLY_LOG_ATTR
    FILE_ONLY_LOG_EXTRA = FILE_ONLY_LOG_EXTRA

    def __init__(
        self,
        level: int | str = "INFO",
        format_string: Optional[str] = None,
        date_format: Optional[str] = None,
        log_file: Optional[Path] = None,
        *,
        console_level: int | str | None = None,
        file_level: int | str | None = None,
    ) -> None:
        """Initialise and apply logging configuration.

        Args:
            level: Root logger level (``INFO`` normally, ``DEBUG`` in debug
                mode).
            format_string: Log formatter string.
            date_format: Date formatter string.
            log_file: Optional path for an initial file handler.
            console_level: Independent console threshold.  Defaults to
                ``DEBUG`` when ``level`` is DEBUG, otherwise WARNING.
            file_level: Independent file threshold.  Defaults to ``level``.
        """

        self.level = _coerce_level(level)
        self.format_string = format_string or self.DEFAULT_FORMAT
        self.date_format = date_format or self.DEFAULT_DATE_FORMAT
        self.log_file = Path(log_file) if log_file is not None else None
        self.console_level = (
            _coerce_level(console_level)
            if console_level is not None
            else (logging.DEBUG if self.level <= logging.DEBUG else logging.WARNING)
        )
        self.file_level = (
            _coerce_level(file_level) if file_level is not None else self.level
        )

        # Apply logging immediately, matching the historical constructor
        # behaviour used by ``setup_*_logging``.
        self._configure_logging()

    def _formatter(self) -> logging.Formatter:
        return logging.Formatter(self.format_string, self.date_format)

    def _add_standard_filters(self, handler: logging.Handler, *, console: bool) -> None:
        # Spotipy filtering historically applied at the root logger.  Keeping
        # it on handlers as well ensures propagated child records are filtered
        # without relying on logger-level filter propagation details.
        handler.addFilter(SpotipyRateLimitFilter())
        if console:
            handler.addFilter(
                FileOnlyLogFilter(include_file_only=self.level <= logging.DEBUG)
            )

    def _configure_logging(self) -> None:
        """Apply root and handler levels, replacing stale application setup."""

        root_logger = logging.getLogger()
        root_logger.setLevel(self.level)

        # This class owns the process-wide application handlers.  Keep the
        # historical reset semantics so repeated setup does not duplicate
        # output (notably when tests/main are re-entered in one process).
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        formatter = self._formatter()

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(self.console_level)
        console_handler.setFormatter(formatter)
        # Alembic's ``fileConfig`` must not replace application handlers once
        # startup logging is configured.  Keep a private marker on handlers so
        # the migration environment can detect this without importing or
        # mutating the application logger configuration.
        setattr(console_handler, "_aoitalk_application_handler", True)
        self._add_standard_filters(console_handler, console=True)
        root_logger.addHandler(console_handler)
        self.console_handler = console_handler

        # ``log_file`` is optional because main.py creates/attaches its
        # timestamped file after log housekeeping has run.
        self.file_handler: logging.Handler | None = None
        if self.log_file is not None:
            self.file_handler = self._create_file_handler(self.log_file, self.file_level)
            root_logger.addHandler(self.file_handler)

        # Preserve the root-level filter for callers that emit directly on
        # the root logger, while handlers cover propagated child records.
        for existing in list(root_logger.filters):
            if isinstance(existing, SpotipyRateLimitFilter):
                root_logger.removeFilter(existing)
        root_logger.addFilter(SpotipyRateLimitFilter())

        self._suppress_noisy_loggers()

    def _create_file_handler(self, file_path: Path, level: int) -> logging.Handler:
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Let FileHandler provide the conventional error for an unusable
            # destination; this path is configuration, not instrumentation.
            pass
        file_handler = logging.FileHandler(file_path, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(self._formatter())
        setattr(file_handler, "_aoitalk_application_handler", True)
        self._add_standard_filters(file_handler, console=False)
        return file_handler

    def _suppress_noisy_loggers(self) -> None:
        """Raise noisy library logger thresholds outside debug mode."""

        target_level = logging.DEBUG if self.level <= logging.DEBUG else logging.INFO
        for logger_name in self.SUPPRESSED_LOGGERS:
            logging.getLogger(logger_name).setLevel(target_level)

    def set_module_level(self, module_name: str, level: int | str) -> None:
        """Set a specific module logger's level."""

        logging.getLogger(module_name).setLevel(_coerce_level(level))

    def add_file_handler(self, file_path: Path, level: int | str | None = None) -> None:
        """Attach an additional file handler.

        ``level=None`` uses the configured file threshold (INFO normally,
        DEBUG in debug mode), rather than the console threshold.
        """

        handler_level = (
            _coerce_level(level) if level is not None else self.file_level
        )
        file_handler = self._create_file_handler(Path(file_path), handler_level)
        logging.getLogger().addHandler(file_handler)
        self.file_handler = file_handler


def setup_default_logging(debug: bool = False) -> LoggingConfig:
    """Set up application logging using the normal/debug output contract."""

    level = logging.DEBUG if debug else logging.INFO
    return LoggingConfig(level=level)


def setup_discord_logging(log_file: Path, debug: bool = False) -> LoggingConfig:
    """Set up logging for the Discord bot."""

    level = logging.DEBUG if debug else logging.INFO
    config = LoggingConfig(level=level, log_file=log_file)

    if debug:
        config.set_module_level("discord", logging.DEBUG)
        config.set_module_level("discord.voice_client", logging.DEBUG)
        config.set_module_level("discord.gateway", logging.INFO)

    return config


__all__ = [
    "FILE_ONLY_LOG_ATTR",
    "FILE_ONLY_LOG_EXTRA",
    "FileOnlyLogFilter",
    "LoggingConfig",
    "SpotipyRateLimitFilter",
    "setup_default_logging",
    "setup_discord_logging",
]
