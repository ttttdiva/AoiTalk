"""Deterministic evidence gate for CrowdWorks opportunity briefs."""

from __future__ import annotations

import hashlib
from typing import Any


MINIMUM_CROWDWORKS_DETAIL_CHARS = 120


def crowdworks_brief_status(source_snapshot: Any, source_text: Any) -> str:
    """Return complete only for a bounded, verified detailed-page capture.

    Listing-card text and legacy manually-created rows are not enough evidence
    for evaluation or an application draft. This is an evidence-presence gate;
    the normal evaluation must still identify missing requirements.
    """
    if not isinstance(source_snapshot, dict):
        return "unverified"
    if source_snapshot.get("detail_capture_status") != "complete":
        return "unverified"
    if not isinstance(source_text, str):
        return "unverified"
    normalized = " ".join(source_text.split())
    if len(normalized) < MINIMUM_CROWDWORKS_DETAIL_CHARS:
        return "unverified"
    chars = source_snapshot.get("detail_text_chars")
    if type(chars) is not int or chars != len(normalized):
        return "unverified"
    digest = source_snapshot.get("detail_text_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        return "unverified"
    if hashlib.sha256(normalized.encode("utf-8")).hexdigest() != digest:
        return "unverified"
    return "complete"


__all__ = ["MINIMUM_CROWDWORKS_DETAIL_CHARS", "crowdworks_brief_status"]
