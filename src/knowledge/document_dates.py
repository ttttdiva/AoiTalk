"""Deterministic Knowledge document dates, independent of application startup.

Ingestion accepts offset-free *explicit metadata* as UTC for compatibility.
Legacy repair must not apply that convention to a historical filesystem mtime
or a GROWI timestamp whose original offset has already been lost.

This module uses only the standard library and performs no I/O. Migration code
can load this file directly with importlib.util without executing the eager
``src.knowledge`` package initializer (which imports runtime services). A
released migration should freeze the repair policy rather than depend on future
changes to the live ingestion policy.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from os import PathLike, fspath
from typing import Any, NamedTuple


DOCUMENT_DATE_FRONTMATTER_KEYS = (
    "date",
    "document_date",
    "documentDate",
    "published_at",
    "publishedAt",
    "published",
    "created_at",
    "createdAt",
    "created",
    "updated_at",
    "updatedAt",
    "updated",
    "lastmod",
    "last_modified",
)
GROWI_DOCUMENT_DATE_KEYS = (
    "updated_at",
    "updatedAt",
    "updated",
    "last_updated_at",
    "lastUpdatedAt",
)
UNKNOWN_LEGACY_DATE_SOURCE = "unknown"
KNOWLEDGE_DOCUMENT_DATE_SOURCES = frozenset(
    {"frontmatter", "filename", "growi", "modified_at", UNKNOWN_LEGACY_DATE_SOURCE}
)


class DocumentDateRepair(NamedTuple):
    """Persist the first two fields; arrange source resync when the third is true."""

    document_date: datetime | None
    document_date_source: str
    needs_resync: bool


def parse_document_datetime(
    value: Any, *, require_offset: bool = False
) -> datetime | None:
    """Normalize supported dates to UTC, optionally requiring an original offset.

    ISO datetimes, ISO/slash/compact calendar dates, and date/datetime objects
    retain the existing ingestion semantics. Date-only and naive values mean
    midnight/clock time in UTC, never the host timezone. With ``require_offset``
    those ambiguous timestamp inputs are rejected instead.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, time.min)
        else:
            raw = str(value).strip()
            if not raw:
                return None
            if re.fullmatch(r"\d{8}", raw):
                parsed = datetime.strptime(raw, "%Y%m%d")
            else:
                try:
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    parsed = datetime.combine(
                        date.fromisoformat(raw.replace("/", "-")), time.min
                    )
        if parsed.utcoffset() is None:
            if require_offset:
                return None
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def frontmatter_document_date(frontmatter: Any) -> datetime | None:
    """Find the first valid allowlisted key in root, metadata, then meta."""
    if not isinstance(frontmatter, dict):
        return None
    candidates = [frontmatter]
    for key in ("metadata", "meta"):
        nested = frontmatter.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for mapping in candidates:
        for key in DOCUMENT_DATE_FRONTMATTER_KEYS:
            parsed = parse_document_datetime(mapping.get(key))
            if parsed is not None:
                return parsed
    return None


def filename_document_date(path: str | PathLike[str]) -> datetime | None:
    """Read a date in the basename, treating Windows/POSIX paths identically."""
    name = fspath(path).replace("\\", "/").rsplit("/", 1)[-1]
    match = re.search(
        r"(?<!\d)(?:(\d{4})[-_.](\d{1,2})[-_.](\d{1,2})|(\d{4})(\d{2})(\d{2}))(?!\d)",
        name,
    )
    if match is None:
        return None
    indices = (1, 2, 3) if match.group(1) else (4, 5, 6)
    try:
        year, month, day = (int(match.group(index)) for index in indices)
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def growi_document_date(
    frontmatter: Any, *, require_offset: bool = False
) -> datetime | None:
    """Read reserved GROWI metadata, preserving all existing timestamp aliases."""
    if not isinstance(frontmatter, dict):
        return None
    growi = frontmatter.get("growi")
    if not isinstance(growi, dict):
        return None
    for key in GROWI_DOCUMENT_DATE_KEYS:
        parsed = parse_document_datetime(growi.get(key), require_offset=require_offset)
        if parsed is not None:
            return parsed
    return None


def _metadata_document_date(
    frontmatter: Any,
    path: str | PathLike[str],
    *,
    growi_updated_at: Any,
    require_growi_offset: bool,
) -> tuple[datetime | None, str | None]:
    explicit = frontmatter_document_date(frontmatter)
    if explicit is not None:
        return explicit, "frontmatter"
    filename = filename_document_date(path)
    if filename is not None:
        return filename, "filename"
    growi = parse_document_datetime(
        growi_updated_at, require_offset=require_growi_offset
    )
    if growi is None:
        growi = growi_document_date(frontmatter, require_offset=require_growi_offset)
    if growi is not None:
        return growi, "growi"
    return None, None


def derive_document_date(
    frontmatter: Any,
    path: str | PathLike[str],
    *,
    growi_updated_at: Any = None,
    modified_at: Any = None,
) -> tuple[datetime | None, str | None]:
    """Ingest dates in order: frontmatter, filename, GROWI, then known mtime.

    ``modified_at`` must come from a trusted current source, e.g. an aware UTC
    datetime constructed from stat.st_mtime. Naive values retain compatibility
    with callers supplying known UTC; this is NOT a legacy repair function.
    """
    value, origin = _metadata_document_date(
        frontmatter, path, growi_updated_at=growi_updated_at, require_growi_offset=False
    )
    if value is not None:
        return value, origin
    fallback = parse_document_datetime(modified_at)
    return (fallback, "modified_at") if fallback is not None else (None, None)


def repair_unknown_legacy_date(
    frontmatter: Any,
    path: str | PathLike[str],
    *,
    growi_updated_at: Any = None,
    modified_at: Any = None,
) -> DocumentDateRepair:
    """Repair an untrusted legacy fallback using independently retained evidence.

    Pass original persisted frontmatter/path/GROWI metadata, not an already
    backfilled document_date. Explicit frontmatter and filename calendar dates
    use the established metadata convention; GROWI requires an original offset.

    ``modified_at`` is accepted for row-oriented callers but deliberately never
    used: the legacy column records neither its writer's timezone nor whether
    it has since been normalized. Attaching tzinfo cannot restore that evidence.
    Re-read the source and use derive_document_date for a proven current mtime.

    The caller selects unknown/legacy-fallback rows; do not overwrite already
    verified semantic dates indiscriminately. Persist ``unknown`` provenance
    even when the legacy modified_at is missing, and schedule source resync.
    """
    value, origin = _metadata_document_date(
        frontmatter, path, growi_updated_at=growi_updated_at, require_growi_offset=True
    )
    if value is None:
        return DocumentDateRepair(None, UNKNOWN_LEGACY_DATE_SOURCE, True)
    assert origin is not None
    return DocumentDateRepair(value, origin, False)


# Direct aliases let KnowledgeService keep its established static helper names.
_parse_document_datetime = parse_document_datetime
_frontmatter_document_date = frontmatter_document_date
_filename_document_date = filename_document_date
_growi_document_date = growi_document_date
_derive_document_date = derive_document_date
