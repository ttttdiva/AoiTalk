"""Safe CrowdWorks exception diagnostics."""

from __future__ import annotations

import re
import traceback
from pathlib import Path


_SCAN_EXCEPTION_CLASS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
_SCAN_SOURCE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}\.py:\d{1,6}$")
_SCAN_SQLSTATE_RE = re.compile(r"^[0-9A-Z]{5}$")


def _safe_scan_exception_diagnostics(error: BaseException) -> dict[str, str | None]:
    """Project only an exception class, basename:line, and SQLSTATE."""
    candidates = (
        getattr(error, "orig", None),
        error,
        getattr(error, "__cause__", None),
        getattr(error, "__context__", None),
    )
    exception_class = "other"
    sqlstate = None
    for candidate in candidates:
        if candidate is None:
            continue
        candidate_name = type(candidate).__name__
        if (
            exception_class == "other"
            and isinstance(candidate, BaseException)
            and _SCAN_EXCEPTION_CLASS_RE.fullmatch(candidate_name)
        ):
            exception_class = candidate_name
        if sqlstate:
            continue
        for attr in ("sqlstate", "pgcode"):
            try:
                value = getattr(candidate, attr, None)
            except Exception:
                value = None
            if isinstance(value, str) and _SCAN_SQLSTATE_RE.fullmatch(value.upper()):
                sqlstate = value.upper()
                break

    source = None
    frames = traceback.extract_tb(error.__traceback__) if error.__traceback__ else []
    if frames:
        frame = frames[-1]
        basename = Path(frame.filename).name
        candidate_source = f"{basename}:{frame.lineno}"
        if _SCAN_SOURCE_RE.fullmatch(candidate_source):
            source = candidate_source
    return {
        "exception_class": exception_class,
        "exception_source": source,
        "sqlstate": sqlstate,
    }
