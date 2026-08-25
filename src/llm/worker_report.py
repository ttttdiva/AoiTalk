"""Structured result contract for Agent Team worker runs.

Workers historically returned an arbitrary string.  The coordinator still
needs to accept that shape, but durable run metadata and parent decisions need
bounded, machine-readable evidence.  This module provides a deliberately
small normalizer at that boundary; it does not change provider routing or
attempt to parse a worker's full transcript.

The publication metadata is intentionally parent-owned.  A child may report
that a change exists, but it cannot mark the report as published or grant
itself commit/push authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping


WORKER_REPORT_SCHEMA_VERSION = "worker_report.v1"
PARENT_PUBLICATION_OWNER = "parent_controller"


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _bounded(value: Any, *, limit: int = 64) -> list[Any]:
    """Normalize a report section to a bounded list without losing mappings."""

    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        values = [value]
    # Reports are stored in AgentRun JSON metadata.  Cap cardinality at the
    # boundary while preserving order; individual strings are clipped too.
    normalized: list[Any] = []
    for item in values[:limit]:
        if isinstance(item, str):
            normalized.append(item[:4000])
        elif isinstance(item, Mapping):
            normalized.append(
                {
                    str(key): (val[:4000] if isinstance(val, str) else val)
                    for key, val in list(item.items())[:32]
                }
            )
        else:
            normalized.append(item)
    return normalized


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))
    except Exception:
        return _text(value)


@dataclass(frozen=True)
class WorkerReport:
    """Short coordinator-facing result returned by one worker instance."""

    task: str = ""
    findings: list[Any] = field(default_factory=list)
    evidence: list[Any] = field(default_factory=list)
    changed_scope: list[Any] = field(default_factory=list)
    verification: list[Any] = field(default_factory=list)
    unresolved: list[Any] = field(default_factory=list)
    decision: Any = None
    references: list[Any] = field(default_factory=list)
    plain_report: str | None = None
    schema_version: str = WORKER_REPORT_SCHEMA_VERSION
    publication: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(
        cls,
        value: "WorkerReport | Mapping[str, Any] | str | None",
        *,
        task: str | None = None,
        parent_run_id: str | None = None,
    ) -> "WorkerReport":
        """Build a typed report through the same legacy-compatible normalizer."""

        return worker_report_object(
            value,
            task=task,
            parent_run_id=parent_run_id,
        )

    @property
    def decision_required(self) -> Any:
        """Compatibility alias for callers using the prose contract name."""

        return self.decision

    @property
    def relevant_references(self) -> list[Any]:
        """Compatibility alias for file/symbol references."""

        return self.references

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe canonical representation."""

        return {
            "schema_version": self.schema_version,
            "task": self.task,
            "findings": _jsonable(self.findings),
            "evidence": _jsonable(self.evidence),
            "changed_scope": _jsonable(self.changed_scope),
            "verification": _jsonable(self.verification),
            "unresolved": _jsonable(self.unresolved),
            "decision": _jsonable(self.decision),
            "references": _jsonable(self.references),
            "plain_report": self.plain_report,
            "publication": _jsonable(self.publication),
        }

    # ``to_dict`` is a convenient compatibility spelling for callers that use
    # existing service/model conventions.
    to_dict = as_dict

    def __str__(self) -> str:
        # Preserve the old worker result when one was supplied as plain text.
        # Structured callers can use ``as_dict`` without reparsing this value.
        return self.plain_report or json.dumps(self.as_dict(), ensure_ascii=False)


def parent_publication_metadata(
    *,
    parent_run_id: str | None = None,
    state: str = "pending",
    publication_ref: str | None = None,
) -> dict[str, Any]:
    """Return the immutable-by-worker publication contract.

    ``state='pending'`` is the only state a worker can emit.  A parent may
    later use :func:`approve_worker_publication` after its own review, but a
    child cannot turn this into an approved/published record.
    """

    requested_state = _text(state).lower() or "pending"
    if requested_state not in {"pending", "approved", "published", "rejected"}:
        requested_state = "pending"
    # The caller may pass a state for display, but worker-facing metadata is
    # fail-closed unless an explicit parent gate later approves it.
    safe_state = requested_state if requested_state == "pending" else "pending"
    return {
        "owner": PARENT_PUBLICATION_OWNER,
        "state": safe_state,
        "worker_can_publish": False,
        "publication_allowed": False,
        "commit_allowed": False,
        "push_allowed": False,
        "parent_gate_required": True,
        "parent_run_id": _text(parent_run_id) or None,
        "publication_ref": _text(publication_ref) or None,
    }


def approve_worker_publication(
    report: WorkerReport | Mapping[str, Any] | str | None,
    *,
    parent_run_id: str,
    publication_ref: str | None = None,
) -> dict[str, Any]:
    """Mark a normalized report as parent-approved without granting workers.

    This is intentionally a pure metadata operation.  It does not execute Git
    or publish anything; the parent controller remains responsible for those
    actions and may persist the returned metadata after its own checks.
    """

    clean_parent_run_id = _text(parent_run_id)
    if not clean_parent_run_id:
        raise PermissionError("Parent run identity is required to approve publication")
    normalized = normalize_worker_report(report)
    normalized["publication"] = {
        "owner": PARENT_PUBLICATION_OWNER,
        "state": "approved",
        "worker_can_publish": False,
        "publication_allowed": True,
        "commit_allowed": False,
        "push_allowed": False,
        "parent_gate_required": True,
        "approved_by_parent_run_id": clean_parent_run_id,
        "publication_ref": _text(publication_ref) or None,
    }
    return normalized


def normalize_worker_report(
    value: WorkerReport | Mapping[str, Any] | str | None,
    *,
    task: str | None = None,
    parent_run_id: str | None = None,
) -> dict[str, Any]:
    """Normalize structured or legacy plain worker output.

    Existing plain reports are retained verbatim in ``plain_report`` and are
    not forced through a lossy parser.  Structured mapping keys are accepted
    as-is for the required fields, with a few harmless aliases used by older
    providers (``changed_files``, ``tests``, ``unresolved_questions``).
    """

    if isinstance(value, WorkerReport):
        result = value.as_dict()
        if task and not result.get("task"):
            result["task"] = _text(task)
        if parent_run_id:
            result["publication"] = parent_publication_metadata(
                parent_run_id=parent_run_id,
            )
        return result

    structured_keys = {
        "task",
        "findings",
        "evidence",
        "changed_scope",
        "verification",
        "unresolved",
        "decision",
        "references",
        "relevant_files",
        "relevant_symbols",
    }
    raw_mapping = dict(value) if isinstance(value, Mapping) else None
    plain = None if raw_mapping is not None else (_text(value) or None)
    # Native providers often return a JSON object as a text completion. Parse
    # only when it is recognizably a WorkerReport so arbitrary legacy prose is
    # still retained verbatim.
    if raw_mapping is None and isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, Mapping) and parsed.keys() & structured_keys:
            raw_mapping = dict(parsed)
            plain = None
    source = raw_mapping or {}
    has_structured = bool(source.keys() & structured_keys)
    if not has_structured and raw_mapping:
        # A provider occasionally returns ``{"output": "..."}`` while still
        # using the legacy contract.  Keep that output in plain_report.
        plain_value = source.get("plain_report", source.get("output", source.get("report")))
        plain = _text(plain_value) or None

    references = _bounded(source.get("references"))
    references.extend(_bounded(source.get("relevant_files")))
    references.extend(_bounded(source.get("relevant_symbols")))
    report = {
        "schema_version": WORKER_REPORT_SCHEMA_VERSION,
        "task": _text(source.get("task") or task),
        "findings": _bounded(source.get("findings")),
        "evidence": _bounded(source.get("evidence")),
        "changed_scope": _bounded(
            source.get("changed_scope", source.get("changed_files"))
        ),
        "verification": _bounded(
            source.get("verification", source.get("tests"))
        ),
        "unresolved": _bounded(
            source.get("unresolved", source.get("unresolved_questions"))
        ),
        "decision": _jsonable(source.get("decision")),
        "references": references[:64],
        "plain_report": plain,
        "publication": parent_publication_metadata(parent_run_id=parent_run_id),
    }
    return report


def worker_report_object(
    value: WorkerReport | Mapping[str, Any] | str | None,
    *,
    task: str | None = None,
    parent_run_id: str | None = None,
) -> WorkerReport:
    """Return the dataclass form for typed callers."""

    normalized = normalize_worker_report(value, task=task, parent_run_id=parent_run_id)
    return WorkerReport(
        task=normalized["task"],
        findings=normalized["findings"],
        evidence=normalized["evidence"],
        changed_scope=normalized["changed_scope"],
        verification=normalized["verification"],
        unresolved=normalized["unresolved"],
        decision=normalized["decision"],
        references=normalized["references"],
        plain_report=normalized["plain_report"],
        schema_version=normalized["schema_version"],
        publication=normalized["publication"],
    )


__all__ = [
    "PARENT_PUBLICATION_OWNER",
    "WORKER_REPORT_SCHEMA_VERSION",
    "WorkerReport",
    "approve_worker_publication",
    "normalize_worker_report",
    "parent_publication_metadata",
    "worker_report_object",
]
