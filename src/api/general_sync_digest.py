"""Content acknowledgements for bounded general sync, never time watermarks.

The digest acknowledges the pages actually delivered. A later run may skip a
table only when its complete projection fits in one bounded page and matches
the client digest. Multi-page tables use ordinary lossless pagination.
Application timestamps and PostgreSQL XIDs are not commit boundaries; neither
is promoted into an incremental ``since`` value here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any


GENERAL_DIGEST_VERSION = 1
PullPage = Callable[[str | None], Awaitable[dict[str, Any]]]


def valid_general_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _seed(table: str, scope: str) -> str:
    return hashlib.sha256(
        f"general-projection:{GENERAL_DIGEST_VERSION}:{table}:{scope}".encode()
    ).hexdigest()


def _extend(previous: str, page: dict[str, Any]) -> str:
    # Include the entire serialized projection, including derived fields and
    # deletion/reconciliation metadata. Never replace this with count/max(time)
    # or a hash of just the base model's timestamps. Cursors are transport only.
    projection = {
        key: value
        for key, value in page.items()
        if key not in {"cursor", "general_digest", "general_not_modified"}
    }
    # This is a set with no SQL ORDER BY, unlike ordered values inside rows.
    if "authoritative_ids" in projection:
        projection["authoritative_ids"] = sorted(projection["authoritative_ids"])
    encoded = json.dumps(
        projection, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(bytes.fromhex(previous) + encoded).hexdigest()


def _cursor_payload(cursor: str) -> dict[str, Any]:
    try:
        decoded = json.loads(base64.urlsafe_b64decode(
            (cursor + "=" * (-len(cursor) % 4)).encode("ascii")
        ).decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("Invalid general sync digest cursor")
        return decoded
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise ValueError("Invalid general sync digest cursor") from exc


def _previous(cursor: str | None, table: str, scope: str) -> str | None:
    if cursor is None:
        return _seed(table, scope)
    envelope = _cursor_payload(cursor)
    if "general_digest" not in envelope:
        # A run resumed from an older client's checkpoint has no trustworthy
        # prefix. Finish it normally, but never acknowledge its partial digest.
        return None
    digest = envelope.get("general_digest")
    if (
        envelope.get("general_digest_version") != GENERAL_DIGEST_VERSION
        or envelope.get("general_digest_scope") != scope
        or envelope.get("table") != table
        or not valid_general_digest(digest)
    ):
        raise ValueError("Invalid general sync digest cursor")
    return digest


def _with_digest_cursor(cursor: str, digest: str, scope: str) -> str:
    envelope = _cursor_payload(cursor)
    envelope.update({
        "general_digest_version": GENERAL_DIGEST_VERSION,
        "general_digest": digest,
        "general_digest_scope": scope,
    })
    return base64.urlsafe_b64encode(json.dumps(
        envelope, separators=(",", ":"),
    ).encode("utf-8")).decode("ascii").rstrip("=")


async def pull_general_digest_page(
    *, table: str, scope: str, cursor: str | None,
    client_digest: str | None, pull_page: PullPage,
) -> dict[str, Any]:
    """Return a delivered-page acknowledgement or a verified unchanged table.

The caller must use a full (since=None) bounded pull with a fixed snapshot upper
bound and the same current ACL as ordinary sync. Only first-page requests may
short-circuit, and only when the complete projection fits in one page. Never
preflight-scan subsequent pages in one HTTP request: even an unchanged table
can otherwise run thousands of SQL queries before returning. The continuation
digest remains a hash of delivered pages, not of a separate candidate scan.
"""
    previous = _previous(cursor, table, scope)
    if cursor is None and client_digest is not None and not valid_general_digest(client_digest):
        raise ValueError("Invalid general sync digest")
    page = await pull_page(cursor)
    if previous is None:
        return page
    digest = _extend(previous, page)
    if cursor is None and client_digest == digest and not page.get("cursor"):
        # This is the whole projection, not a prefix. Preserve the useful
        # unchanged shortcut for small/empty tables without scanning later
        # SQL pages for large conversation histories.
        return {
            "changes": [], "tombstones": [], "cursor": None,
            "general_digest": digest, "general_not_modified": True,
        }
    page = dict(page)
    if page.get("cursor"):
        page["cursor"] = _with_digest_cursor(page["cursor"], digest, scope)
    else:
        page["general_digest"] = digest
    return page
