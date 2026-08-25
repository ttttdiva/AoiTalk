"""Deterministic routing for Dreaming memory extraction candidates.

The LLM may suggest a scope, but it is never allowed to choose the storage
boundary by itself.  This module intentionally has no database or ACL calls;
``ScopedMemoryService.upsert_memory`` remains the final project permission
boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID


MemoryDestination = Literal["user", "project", "docs_candidate", "discard"]


@dataclass(frozen=True)
class MemoryRouteDecision:
    """The storage destination and write metadata selected by the router."""

    destination: MemoryDestination
    scope_type: str | None
    project_id: UUID | None
    status: str
    source_type: str


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _project_uuid(value: Any) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _discard(*, source_type: str = "dreaming_auto_discarded") -> MemoryRouteDecision:
    return MemoryRouteDecision(
        destination="discard",
        scope_type=None,
        project_id=None,
        status="rejected",
        source_type=source_type,
    )


def _docs_candidate() -> MemoryRouteDecision:
    return MemoryRouteDecision(
        destination="docs_candidate",
        scope_type=None,
        project_id=None,
        status="candidate",
        source_type="docs_candidate",
    )


def route_extracted_memory(
    *,
    extracted_memory: Any,
    user_id: str,
    project_id: str | UUID | None,
    session_id: str | UUID | None,
) -> MemoryRouteDecision:
    """Route one extractor item without performing persistence or ACL work.

    ``user_id`` and ``session_id`` are accepted as part of the stable routing
    contract.  The decision is deliberately independent of either value so a
    retry cannot route the same candidate differently; project ACL checks are
    performed by the storage service after this function returns.
    """

    del user_id, session_id
    item = extracted_memory if isinstance(extracted_memory, dict) else {}
    action = str(item.get("action") or "upsert").strip().lower()
    content = str(item.get("content") or "").strip()
    evidence = str(item.get("evidence_span") or "").strip()

    # Delete-all intentionally has no content.  It is still routable only when
    # the user supplied an evidence span, just like other explicit operations.
    content_required = action not in {"delete_all"}
    if (content_required and not content) or not evidence:
        return _discard()

    confidence = _number(item.get("confidence"), 0.0)
    importance = _integer(item.get("importance"), 0)
    sensitivity = str(item.get("sensitivity") or "normal").strip().lower()
    status_hint = str(item.get("status") or "").strip().lower()
    reason = str(item.get("reason") or "").strip().lower()
    if (
        confidence < 0.8
        or importance < 6
        or sensitivity != "normal"
        or status_hint in {"reject", "rejected", "discard"}
        or bool(item.get("rejected"))
        or bool(item.get("transient"))
        or bool(item.get("is_transient"))
        or item.get("expires_at")
        or "transient" in reason
    ):
        return _discard()

    intent = str(item.get("scope_intent") or "user").strip().lower()
    if intent not in {"user", "project", "docs_candidate", "discard"}:
        intent = "user"
    explicit = item.get("explicit_evidence") is True
    current_project = _project_uuid(project_id)
    memory_type = str(item.get("memory_type") or "fact").strip().lower()

    if intent == "discard":
        return _discard()
    if intent == "docs_candidate":
        return _docs_candidate()

    if intent == "project":
        # Project intent is never downgraded into a cross-project user memory.
        # Without an active project (or explicit evidence), retain only as a
        # reviewable Docs candidate.  The actual project ACL is checked later.
        if not current_project or not explicit:
            return _docs_candidate()
        return MemoryRouteDecision(
            destination="project",
            scope_type="project",
            project_id=current_project,
            status="candidate",
            source_type="dreaming_auto",
        )

    # Legacy extractors omitted scope_intent.  Treat those as user hints, but
    # never allow a legacy ``memory_type=project`` item to leak cross-project.
    if memory_type == "project":
        return _docs_candidate()

    if intent == "user":
        if explicit and confidence >= 0.90 and importance >= 7:
            return MemoryRouteDecision(
                destination="user",
                scope_type="user",
                project_id=None,
                status="active",
                source_type="dreaming_auto_verified",
            )
        return MemoryRouteDecision(
            destination="user",
            scope_type="user",
            project_id=None,
            status="candidate",
            source_type="dreaming_auto",
        )

    return _discard()


__all__ = ["MemoryRouteDecision", "route_extracted_memory"]
