"""Bounded, read-only source lookup for personal finance image attachments.

Only user-scoped ``workspace_file`` images with bounded PNG, JPEG or WebP
headers are accepted. This module does not decode, sanitize, extract text, or
perform any network/model operation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any
from uuid import UUID

from .personal_finance_image_types import (
    SUPPORTED_MIME_TYPES,
    FinanceImageTypeError,
    inspect_finance_image,
)
from .search_sources import (
    ResolvedSource,
    SearchSourceResolver,
    SourceRef,
    SourceUnavailable,
)
from .personal_async_guard import run_personal_guard


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_SHA256_LOWER_HEX = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ERROR_CODES = frozenset(
    {
        "unsupported_type",
        "mime_mismatch",
        "malformed_header",
        "source_too_large",
        "pixel_limit",
    }
)


class FinanceAttachmentSourceError(RuntimeError):
    """Stable, non-disclosing failure for attachment source resolution."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class FinanceAttachmentEvidence:
    """Verified attachment bytes; paths, titles, URLs and metadata are omitted."""

    source_ref: UUID
    source_sha256: str
    mime_type: str
    byte_size: int
    content: bytes


def _raise(code: str) -> None:
    raise FinanceAttachmentSourceError(code)


def _fstat_signature(fd: int) -> tuple[int, int, int, int, int]:
    info = os.fstat(fd)
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_size),
        int(info.st_mtime_ns),
    )


def _read_bounded(path: Path) -> tuple[bytes, str]:
    """Read no more than MAX_ATTACHMENT_BYTES + 1 and detect concurrent change."""

    with path.open("rb") as handle:
        before = _fstat_signature(handle.fileno())
        if not stat.S_ISREG(before[2]):
            _raise("source_unavailable")
        if before[3] == 0:
            _raise("source_unavailable")
        if before[3] > MAX_ATTACHMENT_BYTES:
            _raise("source_too_large")

        data = handle.read(MAX_ATTACHMENT_BYTES + 1)
        after = _fstat_signature(handle.fileno())

    if before != after:
        _raise("source_changed")
    if not data:
        _raise("source_unavailable")
    if len(data) > MAX_ATTACHMENT_BYTES:
        _raise("source_too_large")

    return data, hashlib.sha256(data).hexdigest()


def _scope_is_personal(resolver: SearchSourceResolver, owner_user_id: UUID) -> bool:
    scope = getattr(resolver, "scope", None)
    if scope is None:
        return False
    if getattr(scope, "user_id", None) != owner_user_id:
        return False
    if getattr(scope, "project_scope_restricted", False) is not True:
        return False
    return getattr(scope, "project_id", None) is None


def _metadata_matches(
    metadata: object,
    *,
    owner_user_id: UUID,
    expected_sha256: str,
) -> None:
    if not isinstance(metadata, Mapping):
        _raise("source_unavailable")
    if metadata.get("files_scope") != "user":
        _raise("source_unavailable")
    if str(metadata.get("files_owner_id")) != str(owner_user_id):
        _raise("source_unavailable")
    if metadata.get("content_hash") != expected_sha256:
        _raise("source_hash_mismatch")


async def resolve_finance_attachment_source(
    resolver: SearchSourceResolver,
    *,
    owner_user_id: UUID,
    source_ref: UUID,
    expected_sha256: str,
    guard: Callable[[], Any] | None = None,
) -> FinanceAttachmentEvidence:
    """Resolve one user-owned image file with bounded verification.

    ``source_ref`` is intentionally opaque: only a UUID is accepted.  The
    resolver is called exactly once for a valid request, and its returned path
    is the sole filesystem source used here.
    """

    if not isinstance(source_ref, UUID):
        _raise("invalid_source_ref")
    if (
        not isinstance(expected_sha256, str)
        or _SHA256_LOWER_HEX.fullmatch(expected_sha256) is None
    ):
        _raise("invalid_expected_hash")
    if not _scope_is_personal(resolver, owner_user_id):
        _raise("source_unavailable")

    requested_ref = SourceRef("workspace_file", str(source_ref))
    try:
        resolved: ResolvedSource = await resolver.resolve(
            requested_ref,
            open_file=True,
        )
    except SourceUnavailable:
        raise FinanceAttachmentSourceError("source_unavailable") from None
    if guard is not None:
        # Trusted hook: re-check turn/binding/policy right after the await, before any
        # metadata, path or byte access.
        await run_personal_guard(guard)

    if not isinstance(resolved, ResolvedSource):
        _raise("source_unavailable")

    metadata = resolved.metadata
    _metadata_matches(
        metadata,
        owner_user_id=owner_user_id,
        expected_sha256=expected_sha256,
    )
    claimed_mime = metadata.get("mime_type")
    if (
        not isinstance(claimed_mime, str)
        or claimed_mime.strip().lower() not in SUPPORTED_MIME_TYPES
    ):
        _raise("unsupported_type")

    if resolved.path is None:
        _raise("source_unavailable")
    try:
        path = Path(resolved.path)
        content, digest = _read_bounded(path)
    except FinanceAttachmentSourceError:
        raise
    except (OSError, TypeError, ValueError):
        raise FinanceAttachmentSourceError("source_unavailable") from None
    await run_personal_guard(guard)

    indexed_hash = metadata.get("content_hash")
    if digest != expected_sha256 or digest != indexed_hash:
        _raise("source_hash_mismatch")

    try:
        image_type = inspect_finance_image(
            content,
            claimed_mime,
            max_bytes=MAX_ATTACHMENT_BYTES,
        )
    except FinanceImageTypeError as exc:
        code = exc.code if exc.code in _IMAGE_ERROR_CODES else "source_unavailable"
        raise FinanceAttachmentSourceError(code) from None

    await run_personal_guard(guard)

    return FinanceAttachmentEvidence(
        source_ref=source_ref,
        source_sha256=digest,
        mime_type=image_type.mime_type,
        byte_size=len(content),
        content=content,
    )
