"""Deterministic routing for Dreaming memory extraction candidates.

The LLM may suggest a scope, but it is never allowed to choose the storage
boundary by itself.  This module intentionally has no database or ACL calls;
``ScopedMemoryService.upsert_memory`` remains the final project permission
boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Literal
from uuid import UUID


MemoryDestination = Literal["user", "project", "docs_candidate", "discard"]


_TRANSIENT_PROJECT_MEMORY_RE = re.compile(
    r"(?:"
    r"\btoday\b|\bthis\s+time\b|\bfor\s+now\b|\bright\s+now\b|"
    r"\btemporary\b|\btemporarily\b|"
    r"\bone[- ](?:off|time)\b|\bincident[- ]only\b|"
    r"\bincident\s+observation\b|"
    r"今日は|今日だけ|今回だけ|今回限り|今だけ|一時的|一時対応|暫定"
    r")",
    re.IGNORECASE,
)
_DURABLE_SCOPE_RE = re.compile(
    r"(?:"
    r"\bfrom\s+now\s+on\b|\bgoing\s+forward\b|"
    r"\bevery\s+(?:day|week|month|year|weekday|weekdays|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    r"今後|これから|毎日|毎週|毎月|毎年|恒久|継続"
    r")",
    re.IGNORECASE,
)
_TRANSIENT_REASON_RE = re.compile(
    r"(?:\btransient\b|\btemporary\b|\btemporarily\b|"
    r"\bone[- ](?:off|time)\b|\bincident[- ]only\b|"
    r"\bincident\s+observation\b|一時的|一時対応|暫定|今回だけ|今日だけ)",
    re.IGNORECASE,
)
_TRANSIENT_NEGATION_PREFIX_RE = re.compile(
    r"(?:\b(?:not|never)\s+(?:a\s+)?|\bnon[- ]?|非)\s*$",
    re.IGNORECASE,
)


def _reason_marks_transient(reason: str) -> bool:
    """Return true only for an affirmative transient reason marker.

    LLM explanations occasionally state the durable rule as ``not transient``.
    A plain substring check would reject those otherwise valid Project facts.
    Keep this parser deliberately narrow; content/evidence still receive the
    broader transient check below.
    """

    for match in _TRANSIENT_REASON_RE.finditer(reason):
        prefix = reason[max(0, match.start() - 32) : match.start()]
        if _TRANSIENT_NEGATION_PREFIX_RE.search(prefix):
            continue
        return True
    return False


@dataclass(frozen=True)
class MemoryRouteDecision:
    """The storage destination and write metadata selected by the router."""

    destination: MemoryDestination
    scope_type: str | None
    project_id: UUID | None
    status: str
    source_type: str


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _integer(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, float):
        # JSON numbers with a fractional component are not valid importance
        # values; truncating 7.9 to 7 could accidentally pass the Project
        # activation threshold.
        if not math.isfinite(value) or not value.is_integer():
            return default
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and not re.fullmatch(r"[+-]?\d+", value.strip()):
        return default
    try:
        integer = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return integer


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
    verified_corroboration: int | bool = 0,
    corroborated_user_evidence_count: int | bool = 0,
    project_auto_enabled: bool | None = None,
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
        or confidence > 1.0
        or importance > 10
        or sensitivity != "normal"
        or status_hint in {"reject", "rejected", "discard"}
        or bool(item.get("rejected"))
        or bool(item.get("transient"))
        or bool(item.get("is_transient"))
        or item.get("expires_at")
        or _reason_marks_transient(reason)
    ):
        return _discard()

    intent = str(item.get("scope_intent") or "user").strip().lower()
    if intent not in {"user", "project", "docs_candidate", "discard"}:
        intent = "user"
    explicit = item.get("explicit_evidence") is True
    # Consolidation can verify the same fact across independent source turns.
    # Keep the historical explicit-evidence path untouched while allowing a
    # caller to provide that stronger corroboration signal.  ``True`` is
    # accepted as a compatibility shorthand for two verified observations.
    def _corroboration_count(value: Any) -> int:
        try:
            if isinstance(value, bool):
                return 2 if value else 0
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    corroboration_count = max(
        _corroboration_count(verified_corroboration),
        _corroboration_count(corroborated_user_evidence_count),
    )
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
        # A transient Project observation must not become durable memory even
        # when the extractor marks it explicit/high-confidence.  Recurring or
        # otherwise durable scope markers override the transient wording
        # (for example, "today's report is temporary" versus "from now on").
        # ``reason`` has already gone through the negation-aware transient
        # parser above.  Including it in this raw substring check would turn
        # safe explanations such as "not temporary" into false discards.
        project_scope_text = f"{content}\n{evidence}"
        if (
            _TRANSIENT_PROJECT_MEMORY_RE.search(project_scope_text)
            and not _DURABLE_SCOPE_RE.search(project_scope_text)
        ):
            return _discard()
        # A project candidate may be promoted to an active memory only when a
        # trusted caller has already validated the Project's auto-memory
        # setting.  Keep this function pure: arbitrary truthy values from an
        # extractor/request must not grant activation authority.  Lower
        # quality explicit candidates remain reviewable candidates even when
        # the setting is enabled.
        project_verified = (
            project_auto_enabled is True
            and confidence >= 0.90
            and importance >= 7
        )
        return MemoryRouteDecision(
            destination="project",
            scope_type="project",
            project_id=current_project,
            status="active" if project_verified else "candidate",
            source_type=(
                "dreaming_auto_verified" if project_verified else "dreaming_auto"
            ),
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
        if corroboration_count >= 2 and confidence >= 0.90 and importance >= 7:
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
