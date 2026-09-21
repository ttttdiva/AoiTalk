"""Audit and transactionally reconcile the Knowledge Capture inbox.

The command is intentionally conservative.  It is a read-only audit by
default.  A write requires an operator-supplied, digest-frozen plan produced
by the audit.  The audit covers every row in the Knowledge Capture candidate,
question, and notification namespaces, including terminal/non-pending rows;
``limit`` is only a page size and is never a result cap.

Only these durable state transitions are available:

* ``keep``
* ``cancel_notification``
* ``dismiss_question``
* ``discard_candidate``

No row is hard-deleted.  The apply path re-audits, acquires deterministic row
locks, compares every target snapshot, and mutates only the exact KC rows
whose snapshots were frozen in the plan.  Any drift rolls the transaction
back before a commit can happen.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import inspect
import json
import re
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.memory.database import get_database_manager  # noqa: E402
from src.memory.models import (  # noqa: E402
    KnowledgeCaptureCandidate,
    KnowledgeCaptureQuestion,
    NotificationDelivery,
    Task,
)
from src.memory.models.knowledge_capture import (  # noqa: E402
    KNOWLEDGE_CAPTURE_CANDIDATE_STATES,
    KNOWLEDGE_CAPTURE_CANDIDATE_TERMINAL_STATES,
    KNOWLEDGE_CAPTURE_QUESTION_STATES,
    validate_candidate_transition,
)
from src.services.task_management.notifications import (  # noqa: E402
    KNOWLEDGE_CAPTURE_NOTIFICATION_TYPES,
    is_knowledge_capture_notification,
)


SCHEMA_VERSION = 2
PLAN_VERSION = 2
DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 20_000
MAX_SAFE_TEXT_CHARS = 512
MAX_SAFE_JSON_ITEMS = 32
MAX_SAFE_JSON_DEPTH = 3
MAX_SAFE_EVIDENCE_REFS = 32
MAX_JUSTIFICATION_CHARS = 360

ACTION_KEEP = "keep"
ACTION_CANCEL_NOTIFICATION = "cancel_notification"
ACTION_DISMISS_QUESTION = "dismiss_question"
ACTION_DISCARD_CANDIDATE = "discard_candidate"
ALLOWED_ACTIONS = frozenset(
    {
        ACTION_KEEP,
        ACTION_CANCEL_NOTIFICATION,
        ACTION_DISMISS_QUESTION,
        ACTION_DISCARD_CANDIDATE,
    }
)

# Accept names emitted by an earlier operator scaffold, but never emit them.
LEGACY_ACTION_ALIASES = {
    "cancel_knowledge_capture_notification": ACTION_CANCEL_NOTIFICATION,
    "dismiss_pending_question": ACTION_DISMISS_QUESTION,
}

KC_TYPES = frozenset(KNOWLEDGE_CAPTURE_NOTIFICATION_TYPES)
TERMINAL_CANDIDATE_STATES = frozenset(
    str(value).casefold() for value in KNOWLEDGE_CAPTURE_CANDIDATE_TERMINAL_STATES
)
KNOWN_CANDIDATE_STATES = frozenset(
    str(value).casefold() for value in KNOWLEDGE_CAPTURE_CANDIDATE_STATES
)
KNOWN_QUESTION_STATES = frozenset(
    str(value).casefold() for value in KNOWLEDGE_CAPTURE_QUESTION_STATES
)
TERMINAL_NOTIFICATION_STATES = frozenset({"cancelled", "canceled"})

ALLOWED_USER_QUESTION_KINDS = frozenset(
    {"troubleshooting", "procedure", "setup", "runbook", "decision_playbook"}
)
MIN_SURFACED_REUSE_SCORE = 80
MIN_USER_QUESTION_REUSE_SCORE = 90

ENTITY_TYPES = frozenset({"candidate", "question", "notification"})
# Apply questions/notifications before candidates so discard cleanup is safe.
ENTITY_ORDER = {"question": 0, "notification": 1, "candidate": 2}
_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SECRET_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "private_key",
    "refresh_key",
    "access_key",
    "bearer ",
    "transcript",
    "raw_response",
    "provider_response",
)
_SENSITIVE_URL_RE = re.compile(
    r"(?:https?|ftp)://[^\s]+(?:[?&](?:token|secret|password|key|sig|signature|credential)=|@[^\s/]+)",
    re.IGNORECASE,
)
_SENSITIVE_PATH_RE = re.compile(
    r"(?:^|[\\/])(?:users?|home|tmp|var|etc|appdata)(?:[\\/]|$)",
    re.IGNORECASE,
)
_PERSONAL_CAPTURE_RE = re.compile(
    r"(?:movie|film|entertainment|chronolog|timeline|attendance|watched|watching|saw|"
    r"映画|鑑賞|参加履歴|出席|個人(?:の)?(?:履歴|時系列|日付)|見た日|何日に)",
    re.IGNORECASE,
)


class ReconciliationError(RuntimeError):
    """Base error for an unsafe or incomplete reconciliation operation."""


class AuditCompletenessError(ReconciliationError):
    """The audit could not prove that every row was fetched."""


class PlanValidationError(ValueError, ReconciliationError):
    """A supplied plan is malformed or not drawn from its audit."""


class ReconciliationConflict(ReconciliationError):
    """The database drifted from the frozen audit or plan."""


# Names from the prepared scaffold remain import-compatible for operators who
# already have a dry-run wrapper around this maintenance command.
AuditIncompleteError = AuditCompletenessError
PlanDriftError = ReconciliationConflict


@dataclass(frozen=True)
class AuditOptions:
    """Immutable scope and pagination options."""

    page_size: int = DEFAULT_PAGE_SIZE
    project_id: str | None = None

    def __post_init__(self) -> None:
        try:
            page_size = int(self.page_size)
        except (TypeError, ValueError) as exc:
            raise ValueError("page_size must be an integer") from exc
        if page_size < 1 or page_size > MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        project = _uuid_text(self.project_id)
        if self.project_id not in (None, "") and project is None:
            raise ValueError("project_id must be a UUID")
        object.__setattr__(self, "page_size", page_size)
        object.__setattr__(self, "project_id", project)

    @property
    def limit(self) -> int:
        """Compatibility spelling; this is a page size, not a row cap."""

        return self.page_size

    def to_dict(self) -> dict[str, Any]:
        return {"page_size": self.page_size, "limit": self.page_size, "project_id": self.project_id}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return converted
    raise TypeError("expected a mapping or an object with to_dict()")


def _coerce_options(
    options: AuditOptions | Mapping[str, Any] | None = None,
    *,
    limit: int | None = None,
    page_size: int | None = None,
    project_id: UUID | str | None = None,
) -> AuditOptions:
    if isinstance(options, AuditOptions):
        base_page = options.page_size
        base_project = options.project_id
    elif isinstance(options, Mapping):
        base_page = options.get("page_size", options.get("limit", DEFAULT_PAGE_SIZE))
        base_project = options.get("project_id")
    else:
        base_page = DEFAULT_PAGE_SIZE
        base_project = None
    return AuditOptions(
        page_size=page_size if page_size is not None else limit if limit is not None else base_page,
        project_id=project_id if project_id is not None else base_project,
    )


def _json_default(value: Any) -> str:
    if isinstance(value, (UUID, datetime, date)):
        return str(value)
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def stable_digest(value: Any) -> str:
    """Return a deterministic SHA-256 for a JSON-compatible value."""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


_digest = stable_digest


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _uuid_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        parsed = value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None
    return str(parsed)


def _require_uuid(value: Any, field: str) -> UUID:
    text = _uuid_text(value)
    if text is None:
        raise PlanValidationError(f"{field} must be a UUID")
    return UUID(text)


def _safe_text(value: Any, *, limit: int = MAX_SAFE_TEXT_CHARS) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    if not text:
        return None
    lowered = text.casefold()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return None
    if _SENSITIVE_URL_RE.search(text) or _SENSITIVE_PATH_RE.search(text):
        return None
    return text[:limit]


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    """Return bounded, redacted metadata only; never expose unbounded bodies."""

    if depth > MAX_SAFE_JSON_DEPTH:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, (UUID, datetime, date)):
        return str(value)
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
        if isinstance(value, (set, frozenset)):
            values.sort(key=_canonical_json)
        result: list[Any] = []
        for item in values[:MAX_SAFE_JSON_ITEMS]:
            projected = _safe_json(item, depth=depth + 1)
            if projected is not None:
                result.append(projected)
        return result
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:MAX_SAFE_JSON_ITEMS]:
            key = str(raw_key)[:96]
            if any(marker in key.casefold() for marker in _SECRET_MARKERS):
                continue
            projected = _safe_json(item, depth=depth + 1)
            if projected is not None:
                result[key] = projected
        return result
    return None


def _safe_refs(value: Any) -> list[Any]:
    projected = _safe_json(value or [])
    return projected[:MAX_SAFE_EVIDENCE_REFS] if isinstance(projected, list) else []


def _attr(row: Any, name: str, default: Any = None) -> Any:
    try:
        if isinstance(row, Mapping):
            return row.get(name, default)
        return getattr(row, name, default)
    except Exception:
        return default


def _decrypted_attr(row: Any, name: str) -> tuple[Any, bool]:
    try:
        return _attr(row, name), True
    except Exception:
        return None, False


def _raw_payload(notification: Any) -> Mapping[str, Any]:
    payload = _attr(notification, "payload", None)
    return payload if isinstance(payload, Mapping) else {}


def _typed_namespace_notification(notification: Any) -> bool:
    return str(_attr(notification, "notification_type", "")) in KC_TYPES


def _payload_ids(notification: Any) -> tuple[str | None, str | None, int | None]:
    payload = _raw_payload(notification)
    candidate_id = _uuid_text(payload.get("candidate_id"))
    question_id = _uuid_text(payload.get("question_id"))
    try:
        version = int(payload.get("candidate_version"))
    except (TypeError, ValueError):
        version = None
    return candidate_id, question_id, version if version is not None and version > 0 else None


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _candidate_body_metadata(candidate: Any) -> dict[str, Any]:
    """Extract small semantic fields through encrypted ORM properties."""

    metadata: dict[str, Any] = {}
    draft, draft_readable = _decrypted_attr(candidate, "draft_json")
    research, research_readable = _decrypted_attr(candidate, "research_json")
    if not draft_readable:
        metadata["draft_json_unavailable"] = True
    elif isinstance(draft, Mapping):
        for key in ("knowledge_kind", "semantic_key", "schema_version"):
            safe = _safe_text(draft.get(key), limit=240)
            if safe is not None:
                metadata[key] = safe
    if not research_readable:
        metadata["research_json_unavailable"] = True
    elif isinstance(research, Mapping) and isinstance(research.get("sufficient"), bool):
        metadata["research_exhausted"] = bool(research["sufficient"])
    semantic_key = _safe_text(_attr(candidate, "knowledge_semantic_key"), limit=240)
    if semantic_key is not None and "semantic_key" not in metadata:
        metadata["semantic_key"] = semantic_key
    kind = str(metadata.get("knowledge_kind") or "").casefold()
    if kind in ALLOWED_USER_QUESTION_KINDS:
        metadata["knowledge_kind"] = kind
    else:
        metadata.pop("knowledge_kind", None)
    return metadata


def _task_projection(task: Any | None, *, expected_id: str | None = None) -> dict[str, Any] | None:
    if task is None:
        return {"id": expected_id, "missing": True} if expected_id else None
    metadata = _attr(task, "task_metadata", {})
    return {
        "id": _uuid_text(_attr(task, "id")) or str(_attr(task, "id")),
        "project_id": _uuid_text(_attr(task, "project_id")),
        "title": _safe_text(_attr(task, "title"), limit=240),
        "status": _safe_text(_attr(task, "status"), limit=64),
        "completed_at": _iso(_attr(task, "completed_at")),
        "updated_at": _iso(_attr(task, "updated_at")),
        "metadata": _safe_json(metadata) or {},
        "metadata_digest": stable_digest(metadata or {}),
    }


def _task_identity(task: Any | None, *, expected_id: str | None = None) -> dict[str, Any] | None:
    projection = _task_projection(task, expected_id=expected_id)
    if projection is None:
        return None
    return {
        "id": projection.get("id"),
        "project_id": projection.get("project_id"),
        "status": projection.get("status"),
        "completed_at": projection.get("completed_at"),
        "updated_at": projection.get("updated_at"),
        "metadata_digest": projection.get("metadata_digest"),
        "missing": bool(projection.get("missing")),
    }


def _candidate_ref(candidate: Any) -> str | None:
    return _uuid_text(_attr(candidate, "seed_task_id")) or _uuid_text(_attr(candidate, "task_id"))


def _candidate_projection(candidate: Any, task: Any | None = None) -> dict[str, Any]:
    candidate_id = _uuid_text(_attr(candidate, "id")) or str(_attr(candidate, "id"))
    seed_task_id = _candidate_ref(candidate)
    raw_refs = _attr(candidate, "evidence_refs", []) or []
    lease_owner = _safe_text(_attr(candidate, "lease_owner"), limit=128)
    lease_expires = _attr(candidate, "lease_expires_at")
    try:
        lease_active = bool(lease_owner and lease_expires is not None and lease_expires > datetime.utcnow())
    except TypeError:
        lease_active = False
    lease_token = _attr(candidate, "lease_token")
    return {
        "id": candidate_id,
        "project_id": _uuid_text(_attr(candidate, "project_id")),
        "seed_task_id": seed_task_id,
        "task_id": seed_task_id,
        "status": str(_attr(candidate, "status", "")),
        "terminal_status": str(_attr(candidate, "terminal_status", "")),
        "version": _int_value(_attr(candidate, "version"), 1),
        "mode_snapshot": _safe_text(_attr(candidate, "mode_snapshot"), limit=32),
        "reuse_score": _attr(candidate, "reuse_score"),
        "confidence": _attr(candidate, "confidence"),
        "question_rounds": _int_value(_attr(candidate, "question_rounds")),
        "attempt_count": _int_value(_attr(candidate, "attempt_count")),
        "max_attempts": _int_value(_attr(candidate, "max_attempts"), 5),
        "user_edited": bool(_attr(candidate, "user_edited")),
        "knowledge_semantic_key": _safe_text(_attr(candidate, "knowledge_semantic_key"), limit=240),
        "published_node_id": _uuid_text(_attr(candidate, "published_node_id")),
        "target_node_id": _uuid_text(_attr(candidate, "target_node_id")),
        "target_revision_id": _uuid_text(_attr(candidate, "target_revision_id")),
        "published_revision_id": _uuid_text(_attr(candidate, "published_revision_id")),
        "lease_owner": lease_owner,
        "lease_token_digest": stable_digest(str(lease_token)) if lease_token else None,
        "lease_active": lease_active,
        "lease_expires_at": _iso(lease_expires),
        "heartbeat_at": _iso(_attr(candidate, "heartbeat_at")),
        "next_retry_at": _iso(_attr(candidate, "next_retry_at")),
        "last_error_code": _safe_text(_attr(candidate, "last_error_code"), limit=96),
        "last_error_message": _safe_text(_attr(candidate, "last_error_message"), limit=240),
        "evidence_refs": _safe_refs(raw_refs),
        "evidence_digest": _safe_text(_attr(candidate, "evidence_digest"), limit=64),
        "body_metadata": _candidate_body_metadata(candidate),
        "task": _task_projection(task, expected_id=seed_task_id),
        "created_at": _iso(_attr(candidate, "created_at")),
        "updated_at": _iso(_attr(candidate, "updated_at")),
    }


def _candidate_snapshot(candidate: Any, task: Any | None = None) -> dict[str, Any]:
    projection = _candidate_projection(candidate, task)
    task_identity = _task_identity(task, expected_id=projection.get("seed_task_id"))
    return {
        "id": projection["id"],
        "project_id": projection.get("project_id"),
        "seed_task_id": projection.get("seed_task_id"),
        "status": projection.get("status"),
        "terminal_status": projection.get("terminal_status"),
        "version": projection.get("version"),
        "updated_at": projection.get("updated_at"),
        "completion_fingerprint": _safe_text(_attr(candidate, "completion_fingerprint"), limit=64),
        "mode_snapshot": projection.get("mode_snapshot"),
        "evidence_digest": projection.get("evidence_digest"),
        "evidence_refs_digest": stable_digest(_attr(candidate, "evidence_refs", []) or []),
        "body_metadata_digest": stable_digest(projection.get("body_metadata") or {}),
        "published_node_id": projection.get("published_node_id"),
        "target_node_id": projection.get("target_node_id"),
        "target_revision_id": projection.get("target_revision_id"),
        "published_revision_id": projection.get("published_revision_id"),
        "user_edited": projection.get("user_edited"),
        "lease_owner": projection.get("lease_owner"),
        "lease_token_digest": projection.get("lease_token_digest"),
        "lease_expires_at": projection.get("lease_expires_at"),
        "heartbeat_at": projection.get("heartbeat_at"),
        "next_retry_at": projection.get("next_retry_at"),
        "last_error_code": projection.get("last_error_code"),
        "last_error_message": projection.get("last_error_message"),
        "task_snapshot_digest": stable_digest(task_identity) if task_identity is not None else None,
    }


def _question_projection(question: Any, candidate: Any | None = None) -> dict[str, Any]:
    question_text, question_readable = _decrypted_attr(question, "question")
    title, title_readable = _decrypted_attr(question, "title")
    message, message_readable = _decrypted_attr(question, "message")
    options, options_readable = _decrypted_attr(question, "options_json")
    content = {"question": question_text, "title": title, "message": message, "options": options}
    return {
        "id": _uuid_text(_attr(question, "id")) or str(_attr(question, "id")),
        "candidate_id": _uuid_text(_attr(question, "candidate_id")),
        "project_id": _uuid_text(_attr(question, "project_id")),
        "status": str(_attr(question, "status", "")),
        "round_number": _int_value(_attr(question, "round_number"), 1),
        "candidate_version": _int_value(_attr(question, "candidate_version")) if _attr(question, "candidate_version") is not None else None,
        "version": _int_value(_attr(question, "version"), 1),
        "question": _safe_text(question_text, limit=512),
        "title": _safe_text(title, limit=160),
        "message": _safe_text(message, limit=512) or _safe_text(question_text, limit=512),
        "options": _safe_json(options) if isinstance(options, (list, tuple, Mapping)) else [],
        "encrypted_metadata_available": bool(question_readable and title_readable and message_readable and options_readable),
        "evidence_digest": _safe_text(_attr(question, "evidence_digest"), limit=64),
        "asked_at": _iso(_attr(question, "asked_at")),
        "answered_at": _iso(_attr(question, "answered_at")),
        "dismissed_at": _iso(_attr(question, "dismissed_at")),
        "created_at": _iso(_attr(question, "created_at")),
        "updated_at": _iso(_attr(question, "updated_at")),
        "candidate_snapshot_digest": stable_digest(_candidate_snapshot(candidate)) if candidate is not None else None,
        "content_digest": stable_digest(content),
    }


def _question_snapshot(question: Any, candidate: Any | None = None) -> dict[str, Any]:
    projection = _question_projection(question, candidate)
    return {
        "id": projection["id"],
        "candidate_id": projection.get("candidate_id"),
        "project_id": projection.get("project_id"),
        "status": projection.get("status"),
        "round_number": projection.get("round_number"),
        "candidate_version": projection.get("candidate_version"),
        "version": projection.get("version"),
        "updated_at": projection.get("updated_at"),
        "evidence_digest": projection.get("evidence_digest"),
        "content_digest": projection.get("content_digest"),
        "candidate_snapshot_digest": projection.get("candidate_snapshot_digest"),
    }


def _payload_snapshot(notification: Any) -> dict[str, Any]:
    payload = _raw_payload(notification)
    candidate_id, question_id, version = _payload_ids(notification)
    return {
        "kind": _safe_text(payload.get("kind"), limit=64),
        "candidate_id": candidate_id,
        "question_id": question_id,
        "candidate_version": version,
        "keys": sorted(str(key)[:96] for key in payload),
        "extra_keys": sorted(str(key)[:96] for key in payload if key not in {"kind", "candidate_id", "question_id", "candidate_version"}),
    }


def _notification_projection(notification: Any, candidate: Any | None = None, question: Any | None = None) -> dict[str, Any]:
    candidate_id, question_id, version = _payload_ids(notification)
    payload = _raw_payload(notification)
    candidate_snapshot = _candidate_snapshot(candidate) if candidate is not None else None
    question_snapshot = _question_snapshot(question, candidate) if question is not None else None
    return {
        "id": _uuid_text(_attr(notification, "id")) or str(_attr(notification, "id")),
        "project_id": _uuid_text(_attr(notification, "project_id")),
        "task_id": _uuid_text(_attr(notification, "task_id")),
        "channel": _safe_text(_attr(notification, "channel"), limit=32),
        "notification_type": str(_attr(notification, "notification_type", "")),
        "status": str(_attr(notification, "status", "")),
        "read_at": _iso(_attr(notification, "read_at")),
        "delivered_at": _iso(_attr(notification, "delivered_at")),
        "scheduled_for": _iso(_attr(notification, "scheduled_for")),
        "dedupe_key": _safe_text(_attr(notification, "dedupe_key"), limit=255),
        "title": _safe_text(_attr(notification, "title"), limit=160),
        "message": _safe_text(_attr(notification, "message"), limit=512),
        "payload": _payload_snapshot(notification),
        "payload_digest": stable_digest(payload),
        "candidate_id": candidate_id,
        "question_id": question_id,
        "payload_candidate_version": version,
        "candidate_snapshot_digest": stable_digest(candidate_snapshot) if candidate_snapshot is not None else None,
        "question_snapshot_digest": stable_digest(question_snapshot) if question_snapshot is not None else None,
        "created_at": _iso(_attr(notification, "created_at")),
        "updated_at": _iso(_attr(notification, "updated_at")),
    }


def _notification_snapshot(notification: Any, candidate: Any | None = None, question: Any | None = None) -> dict[str, Any]:
    projection = _notification_projection(notification, candidate, question)
    return {
        "id": projection["id"],
        "project_id": projection.get("project_id"),
        "task_id": projection.get("task_id"),
        "occurrence_id": _uuid_text(_attr(notification, "occurrence_id")),
        "user_id": _uuid_text(_attr(notification, "user_id")),
        "channel": projection.get("channel"),
        "notification_type": projection.get("notification_type"),
        "status": projection.get("status"),
        "dedupe_key": projection.get("dedupe_key"),
        "scheduled_for": projection.get("scheduled_for"),
        "delivered_at": projection.get("delivered_at"),
        "read_at": projection.get("read_at"),
        "title_digest": stable_digest(_attr(notification, "title") or ""),
        "message_digest": stable_digest(_attr(notification, "message") or ""),
        "created_at": projection.get("created_at"),
        "updated_at": projection.get("updated_at"),
        "payload": projection.get("payload"),
        "payload_digest": projection.get("payload_digest"),
        "candidate_id": projection.get("candidate_id"),
        "question_id": projection.get("question_id"),
        "payload_candidate_version": projection.get("payload_candidate_version"),
        "candidate_snapshot_digest": projection.get("candidate_snapshot_digest"),
        "question_snapshot_digest": projection.get("question_snapshot_digest"),
    }


def _status(value: Any) -> str:
    return str(value or "").strip().casefold()


def _status_is_terminal(value: Any) -> bool:
    return _status(value) in TERMINAL_CANDIDATE_STATES


def _reuse_score(candidate: Any | None) -> int | None:
    if candidate is None:
        return None
    try:
        return int(_attr(candidate, "reuse_score")) if _attr(candidate, "reuse_score") is not None else None
    except (TypeError, ValueError):
        return None


def _knowledge_kind(candidate: Any | None, task: Any | None = None) -> str | None:
    if candidate is not None:
        kind = str(_candidate_body_metadata(candidate).get("knowledge_kind") or "").casefold()
        if kind in ALLOWED_USER_QUESTION_KINDS:
            return kind
    metadata = _attr(task, "task_metadata", {})
    if isinstance(metadata, Mapping):
        kind = str(metadata.get("knowledge_kind") or "").casefold()
        if kind in ALLOWED_USER_QUESTION_KINDS:
            return kind
    return None


def _personal_or_incidental(*values: Any) -> bool:
    return any(_PERSONAL_CAPTURE_RE.search(str(value or "")) for value in values)


def _candidate_surface_allowed(candidate: Any | None, task: Any | None) -> bool:
    score = _reuse_score(candidate)
    return bool(score is not None and score >= MIN_SURFACED_REUSE_SCORE and not _personal_or_incidental(_attr(task, "title"), _attr(task, "description"), _attr(candidate, "knowledge_semantic_key")))


def _question_surface_allowed(candidate: Any | None, task: Any | None, question: Any | None) -> bool:
    score = _reuse_score(candidate)
    kind = _knowledge_kind(candidate, task)
    values: list[Any] = []
    if question is not None:
        for name in ("question", "title", "message"):
            value, _ = _decrypted_attr(question, name)
            values.append(value)
    return bool(score is not None and score >= MIN_USER_QUESTION_REUSE_SCORE and kind in ALLOWED_USER_QUESTION_KINDS and not _personal_or_incidental(_attr(task, "title"), _attr(task, "description"), _attr(candidate, "knowledge_semantic_key"), *values))


def _finding(*, entity_type: str, entity_id: str, code: str, severity: str, details: Mapping[str, Any] | None = None, justification: str) -> dict[str, Any]:
    safe_reason = _safe_text(justification, limit=MAX_JUSTIFICATION_CHARS) or code
    return {"entity_type": entity_type, "entity_id": entity_id, "code": code, "severity": severity, "details": _safe_json(dict(details or {})) or {}, "justification": safe_reason}


def _append_finding(findings: list[dict[str, Any]], *, entity_type: str, entity_id: str, code: str, severity: str, details: Mapping[str, Any] | None = None, justification: str) -> None:
    if any(row.get("entity_type") == entity_type and row.get("entity_id") == entity_id and row.get("code") == code for row in findings):
        return
    findings.append(_finding(entity_type=entity_type, entity_id=entity_id, code=code, severity=severity, details=details, justification=justification))


def _scope_condition(model: Any, options: AuditOptions) -> list[Any]:
    return [model.project_id == UUID(options.project_id)] if options.project_id is not None else []


async def _execute(session: Any, statement: Any) -> Any:
    result = session.execute(statement)
    return await result if inspect.isawaitable(result) else result


def _result_all(result: Any) -> list[Any]:
    method = getattr(result, "all", None)
    if callable(method):
        return list(method())
    mappings = getattr(result, "mappings", None)
    if callable(mappings):
        mapped = mappings()
        method = getattr(mapped, "all", None)
        if callable(method):
            return list(method())
    try:
        return list(result)
    except TypeError:
        return []


def _scalar_rows(result: Any) -> list[Any]:
    scalars = getattr(result, "scalars", None)
    if callable(scalars):
        scalar_result = scalars()
        method = getattr(scalar_result, "all", None)
        if callable(method):
            return list(method())
        try:
            return list(scalar_result)
        except TypeError:
            pass
    rows = _result_all(result)
    return [row[0] if isinstance(row, (tuple, list)) and row else row for row in rows]


def _one_scalar(result: Any) -> Any:
    method = getattr(result, "scalar_one_or_none", None)
    if callable(method):
        return method()
    method = getattr(result, "scalar", None)
    if callable(method):
        return method()
    values = _scalar_rows(result)
    return values[0] if values else None


def _row_values(row: Any) -> tuple[Any, Any]:
    if isinstance(row, Mapping):
        return row.get("status"), row.get("count", row.get("count_1", 0))
    if isinstance(row, (tuple, list)):
        return (row[0] if row else None, row[1] if len(row) > 1 else 0)
    return _attr(row, "status"), _attr(row, "count", _attr(row, "count_1", 0))


def _status_counts(rows: Sequence[Any], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = _attr(row, field)
        key = "<null>" if value is None else str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


async def _count_status_rows(session: Any, model: Any, *, options: AuditOptions, label: str, extra_conditions: Sequence[Any] = ()) -> tuple[dict[str, int], int]:
    conditions = [*extra_conditions, *_scope_condition(model, options)]
    grouped = select(model.status, func.count(model.id)).where(*conditions).group_by(model.status).order_by(model.status)
    counts: dict[str, int] = {}
    for row in _result_all(await _execute(session, grouped)):
        raw_status, raw_count = _row_values(row)
        key = "<null>" if raw_status is None else str(raw_status)
        counts[key] = counts.get(key, 0) + int(raw_count or 0)
    total = int(_one_scalar(await _execute(session, select(func.count(model.id)).where(*conditions))) or 0)
    if sum(counts.values()) != total:
        raise AuditCompletenessError(f"{label} count phase is inconsistent")
    return dict(sorted(counts.items())), total


async def _count_kc_notifications(session: Any, *, options: AuditOptions) -> tuple[dict[str, int], int]:
    conditions = [NotificationDelivery.notification_type.in_(tuple(KC_TYPES))]
    conditions.extend(_scope_condition(NotificationDelivery, options))
    grouped = select(NotificationDelivery.status, func.count(NotificationDelivery.id)).where(*conditions).group_by(NotificationDelivery.status).order_by(NotificationDelivery.status)
    counts: dict[str, int] = {}
    for row in _result_all(await _execute(session, grouped)):
        raw_status, raw_count = _row_values(row)
        key = "<null>" if raw_status is None else str(raw_status)
        counts[key] = counts.get(key, 0) + int(raw_count or 0)
    total = int(_one_scalar(await _execute(session, select(func.count(NotificationDelivery.id)).where(*conditions))) or 0)
    if sum(counts.values()) != total:
        raise AuditCompletenessError("KC notification count phase is inconsistent")
    return dict(sorted(counts.items())), total


async def _fetch_all_pages(session: Any, model: Any, *, conditions: Sequence[Any], page_size: int, expected_count: int, label: str) -> list[Any]:
    """Fetch exactly the count-first result set or fail closed."""

    if expected_count < 0:
        raise AuditCompletenessError(f"{label} count is negative")
    rows: list[Any] = []
    seen_ids: set[str] = set()
    offset = 0
    while len(rows) < expected_count:
        statement = select(model).where(*conditions).order_by(model.id).limit(page_size).offset(offset)
        page = _scalar_rows(await _execute(session, statement))
        if not page:
            raise AuditCompletenessError(f"{label} page audit truncated: expected={expected_count} fetched={len(rows)}")
        remaining = expected_count - len(rows)
        if len(page) > remaining:
            raise AuditCompletenessError(f"{label} changed during pagination")
        for row in page:
            row_id = _uuid_text(_attr(row, "id")) or str(_attr(row, "id"))
            if row_id in seen_ids:
                raise AuditCompletenessError(f"{label} pagination repeated row {row_id}")
            seen_ids.add(row_id)
            rows.append(row)
        offset += len(page)
        if len(page) < page_size and len(rows) < expected_count:
            raise AuditCompletenessError(f"{label} page audit truncated: expected={expected_count} fetched={len(rows)}")
    if len(rows) != expected_count:
        raise AuditCompletenessError(f"{label} page audit incomplete")
    return rows


async def _fetch_by_ids(session: Any, model: Any, ids: Sequence[str], *, page_size: int) -> list[Any]:
    unique_ids = sorted({value for value in ids if _uuid_text(value) is not None})
    rows: list[Any] = []
    for start in range(0, len(unique_ids), page_size):
        chunk = unique_ids[start : start + page_size]
        rows.extend(_scalar_rows(await _execute(session, select(model).where(model.id.in_([UUID(value) for value in chunk])).order_by(model.id))))
    return sorted(rows, key=lambda row: _uuid_text(_attr(row, "id")) or str(_attr(row, "id")))


def _id_map(rows: Sequence[Any]) -> dict[str, Any]:
    return {_uuid_text(_attr(row, "id")) or str(_attr(row, "id")): row for row in rows}


def _add_time_findings(findings: list[dict[str, Any]], *, entity_type: str, entity_id: str, row: Any) -> None:
    created = _attr(row, "created_at")
    updated = _attr(row, "updated_at")
    if updated is None:
        _append_finding(findings, entity_type=entity_type, entity_id=entity_id, code=f"{entity_type}_updated_at_missing", severity="actionable", justification="The row has no updated_at timestamp, so freshness cannot be proven.")
    elif not isinstance(updated, (datetime, date)):
        _append_finding(findings, entity_type=entity_type, entity_id=entity_id, code=f"{entity_type}_updated_at_invalid", severity="actionable", justification="The updated_at value is not a valid timestamp.")
    if created is not None and updated is not None:
        try:
            before = created > updated
        except TypeError:
            before = False
            _append_finding(findings, entity_type=entity_type, entity_id=entity_id, code=f"{entity_type}_updated_at_invalid", severity="actionable", justification="The row timestamps cannot be compared safely.")
        if before:
            _append_finding(findings, entity_type=entity_type, entity_id=entity_id, code=f"{entity_type}_updated_at_before_created_at", severity="actionable", justification="The row updated_at precedes created_at.")


def _classify_candidate(candidate: Any, *, task: Any | None, questions: Sequence[Any], notifications: Sequence[Any], findings: list[dict[str, Any]]) -> None:
    entity_id = _uuid_text(_attr(candidate, "id")) or str(_attr(candidate, "id"))
    status = _status(_attr(candidate, "status"))
    finding_severity = "retain_review" if _status_is_terminal(status) else "actionable"
    project_id = _uuid_text(_attr(candidate, "project_id"))
    if project_id is None:
        _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_project_missing", severity=finding_severity, justification="The candidate has no project binding.")
    if status not in KNOWN_CANDIDATE_STATES:
        _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_status_invalid", severity="retain_review", details={"status": _attr(candidate, "status")}, justification="The candidate status is outside the local Knowledge Capture state contract.")
    if _int_value(_attr(candidate, "version"), 0) < 1:
        _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_version_invalid", severity="retain_review", justification="The candidate version fence is missing or non-positive.")
    if _status(_attr(candidate, "terminal_status")) not in {"closed", "cancelled"}:
        _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_terminal_status_invalid", severity="retain_review", details={"terminal_status": _attr(candidate, "terminal_status")}, justification="The candidate terminal_status is outside the local contract.")
    task_id = _candidate_ref(candidate)
    if task_id is None:
        _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_seed_task_orphan", severity=finding_severity, justification="The candidate has no seed Task reference.")
    elif task is None:
        _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_seed_task_missing", severity=finding_severity, details={"seed_task_id": task_id}, justification="The candidate references no existing seed Task.")
    elif _uuid_text(_attr(task, "project_id")) != project_id:
        _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_seed_task_project_mismatch", severity=finding_severity, details={"candidate_project_id": project_id, "task_project_id": _uuid_text(_attr(task, "project_id"))}, justification="The candidate and its seed Task belong to different projects.")
    pending_questions = [
        row for row in questions if _status(_attr(row, "status")) == "pending"
    ]
    if status == "needs_user":
        if not pending_questions:
            _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_needs_user_without_pending_question", severity="actionable", justification="The candidate claims needs_user but has no pending question.")
        if not _question_surface_allowed(
            candidate, task, pending_questions[0] if pending_questions else None
        ):
            _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_below_interruption_policy", severity="actionable", details={"reuse_score": _reuse_score(candidate), "knowledge_kind": _knowledge_kind(candidate, task)}, justification="The candidate is not proven exceptional enough to interrupt the user.")
        if _knowledge_kind(candidate, task) is None:
            _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_non_operational_kind", severity="actionable", justification="The candidate has no approved reusable operational knowledge kind.")
        question_text = []
        for question in pending_questions:
            for field in ("question", "title", "message"):
                value, _ = _decrypted_attr(question, field)
                question_text.append(value)
        if _personal_or_incidental(
            _attr(task, "title"),
            _attr(task, "description"),
            _attr(candidate, "knowledge_semantic_key"),
            *question_text,
        ):
            _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_personal_or_incidental", severity="actionable", justification="Personal or incidental knowledge must not create a user-facing interruption.")
    elif status == "draft_ready":
        if not _candidate_surface_allowed(candidate, task):
            _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_below_interruption_policy", severity="actionable", details={"reuse_score": _reuse_score(candidate)}, justification="The draft candidate is not proven to meet the surfaced reuse threshold.")
        if _personal_or_incidental(
            _attr(task, "title"),
            _attr(task, "description"),
            _attr(candidate, "knowledge_semantic_key"),
        ):
            _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_personal_or_incidental", severity="actionable", justification="Personal or incidental knowledge must not create a user-facing interruption.")
    if _status_is_terminal(status):
        if any(_status(_attr(row, "status")) == "pending" for row in questions):
            _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="terminal_candidate_has_pending_question", severity="actionable", justification="A terminal candidate must not retain a pending question.")
        if any(
            _status(_attr(row, "channel")) == "in_app"
            and _status(_attr(row, "status")) not in TERMINAL_NOTIFICATION_STATES
            for row in notifications
        ):
            _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="terminal_candidate_has_active_notification", severity="actionable", justification="A terminal candidate must not retain an active KC notification.")
    if status == "researching":
        expiry = _attr(candidate, "lease_expires_at")
        if expiry is None or _attr(candidate, "lease_owner") is None:
            _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_researching_without_lease", severity="retain_review", justification="A researching candidate has no complete lease fence.")
        else:
            try:
                stale = expiry <= datetime.utcnow()
            except TypeError:
                stale = False
                _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_stale_lease_timestamp_invalid", severity="retain_review", justification="The candidate lease timestamp cannot be compared safely.")
            if stale:
                _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_stale_lease", severity="retain_review", justification="The researching candidate lease has expired.")
    if _attr(candidate, "evidence_digest") and not _attr(candidate, "evidence_refs"):
        _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_evidence_without_refs", severity="retain_review", justification="The candidate has an evidence digest but no evidence references.")
    evidence_digest = _attr(candidate, "evidence_digest")
    if evidence_digest is not None and not _DIGEST_RE.fullmatch(str(evidence_digest)):
        _append_finding(findings, entity_type="candidate", entity_id=entity_id, code="candidate_evidence_digest_invalid", severity="retain_review", justification="The candidate evidence digest is not a SHA-256 value.")
    _add_time_findings(findings, entity_type="candidate", entity_id=entity_id, row=candidate)


def _classify_question(question: Any, *, candidate: Any | None, task: Any | None, findings: list[dict[str, Any]]) -> None:
    entity_id = _uuid_text(_attr(question, "id")) or str(_attr(question, "id"))
    status = _status(_attr(question, "status"))
    if status not in KNOWN_QUESTION_STATES:
        _append_finding(findings, entity_type="question", entity_id=entity_id, code="question_status_invalid", severity="retain_review", justification="The question status is outside the local Knowledge Capture contract.")
    if _int_value(_attr(question, "version"), 0) < 1:
        _append_finding(findings, entity_type="question", entity_id=entity_id, code="question_version_invalid", severity="retain_review", justification="The question version fence is missing or non-positive.")
    if _int_value(_attr(question, "round_number"), 0) not in {1, 2}:
        _append_finding(findings, entity_type="question", entity_id=entity_id, code="question_round_invalid", severity="retain_review", justification="The question round is outside the two-round contract.")
    candidate_id = _uuid_text(_attr(question, "candidate_id"))
    if candidate is None:
        if status == "pending":
            _append_finding(findings, entity_type="question", entity_id=entity_id, code="orphan_pending_question", severity="actionable", details={"candidate_id": candidate_id}, justification="The pending question has no matching candidate and can be dismissed safely.")
        _add_time_findings(findings, entity_type="question", entity_id=entity_id, row=question)
        return
    if candidate_id != _uuid_text(_attr(candidate, "id")):
        _append_finding(findings, entity_type="question", entity_id=entity_id, code="question_candidate_mismatch", severity="actionable", justification="The question candidate binding is inconsistent.")
    if _uuid_text(_attr(question, "project_id")) != _uuid_text(_attr(candidate, "project_id")):
        _append_finding(findings, entity_type="question", entity_id=entity_id, code="question_project_mismatch", severity="actionable", justification="The question and candidate projects do not match.")
    if status != "pending":
        _add_time_findings(findings, entity_type="question", entity_id=entity_id, row=question)
        return
    if _status_is_terminal(_attr(candidate, "status")):
        _append_finding(findings, entity_type="question", entity_id=entity_id, code="question_terminal_candidate", severity="actionable", justification="A pending question cannot remain on a terminal candidate.")
    candidate_version = _attr(question, "candidate_version")
    candidate_current_version = _int_value(_attr(candidate, "version"), 0)
    if candidate_version is None or _int_value(candidate_version, 0) < 1:
        _append_finding(findings, entity_type="question", entity_id=entity_id, code="question_missing_candidate_version", severity="actionable", justification="The question has no positive candidate version fence.")
    elif _int_value(candidate_version) != candidate_current_version:
        _append_finding(findings, entity_type="question", entity_id=entity_id, code="question_candidate_version_mismatch", severity="actionable", details={"question_candidate_version": _int_value(candidate_version), "candidate_version": candidate_current_version}, justification="The question version fence is stale.")
    candidate_digest = _attr(candidate, "evidence_digest")
    question_digest = _attr(question, "evidence_digest")
    if candidate_digest and question_digest and str(candidate_digest) != str(question_digest):
        _append_finding(findings, entity_type="question", entity_id=entity_id, code="question_evidence_digest_mismatch", severity="actionable", justification="The question evidence digest does not match its candidate.")
    elif candidate_digest and not question_digest:
        _append_finding(findings, entity_type="question", entity_id=entity_id, code="question_evidence_digest_missing", severity="actionable", justification="The question is missing the candidate evidence digest.")
    if _status(_attr(candidate, "status")) != "needs_user":
        _append_finding(findings, entity_type="question", entity_id=entity_id, code="question_candidate_not_needs_user", severity="actionable", justification="Only a candidate in needs_user may retain a pending question.")
    elif _question_surface_allowed(candidate, task, question):
        _append_finding(findings, entity_type="question", entity_id=entity_id, code="question_retained_high_interruption", severity="retain_review", details={"reuse_score": _reuse_score(candidate), "knowledge_kind": _knowledge_kind(candidate, task)}, justification="The question meets the exceptional operational interruption threshold.")
    else:
        _append_finding(findings, entity_type="question", entity_id=entity_id, code="question_below_interruption_policy", severity="actionable", details={"reuse_score": _reuse_score(candidate), "knowledge_kind": _knowledge_kind(candidate, task)}, justification="The question is not proven exceptional, operational, and user-only.")
    _add_time_findings(findings, entity_type="question", entity_id=entity_id, row=question)


def _classify_notification(notification: Any, *, candidate: Any | None, question: Any | None, task: Any | None, findings: list[dict[str, Any]]) -> None:
    entity_id = _uuid_text(_attr(notification, "id")) or str(_attr(notification, "id"))
    notification_type = str(_attr(notification, "notification_type", ""))
    status = _status(_attr(notification, "status"))
    finding_severity = "retain_review" if status in TERMINAL_NOTIFICATION_STATES else "actionable"
    if status == "":
        _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_status_invalid", severity="retain_review", justification="The notification status is empty.")
    if _status(_attr(notification, "channel")) != "in_app":
        if status not in TERMINAL_NOTIFICATION_STATES:
            _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_non_in_app_channel", severity="retain_review", details={"channel": _attr(notification, "channel")}, justification="Only in-app Knowledge Capture notifications are in the user-facing reconciliation scope.")
        _add_time_findings(findings, entity_type="notification", entity_id=entity_id, row=notification)
        return
    if not is_knowledge_capture_notification(notification):
        _append_finding(findings, entity_type="notification", entity_id=entity_id, code="malformed_knowledge_capture_payload", severity="actionable" if status not in TERMINAL_NOTIFICATION_STATES else "retain_review", details={"notification_type": notification_type, "payload": _payload_snapshot(notification)}, justification="The exact KC notification type has a malformed typed payload.")
        _add_time_findings(findings, entity_type="notification", entity_id=entity_id, row=notification)
        return
    candidate_id, question_id, payload_version = _payload_ids(notification)
    if candidate is None:
        _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_missing_candidate", severity="actionable" if status not in TERMINAL_NOTIFICATION_STATES else "retain_review", details={"candidate_id": candidate_id}, justification="The typed notification points to no existing candidate.")
        _add_time_findings(findings, entity_type="notification", entity_id=entity_id, row=notification)
        return
    if _uuid_text(_attr(notification, "project_id")) != _uuid_text(_attr(candidate, "project_id")):
        _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_candidate_project_mismatch", severity=finding_severity, justification="The notification and candidate projects do not match.")
    if _status_is_terminal(_attr(candidate, "status")) and status not in TERMINAL_NOTIFICATION_STATES:
        _append_finding(findings, entity_type="notification", entity_id=entity_id, code="terminal_candidate_has_active_notification", severity="actionable", justification="The notification belongs to a terminal candidate.")
    candidate_version = _int_value(_attr(candidate, "version"), 0)
    if payload_version is None or payload_version != candidate_version:
        _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_stale_candidate_version", severity=finding_severity, details={"payload_candidate_version": payload_version, "candidate_version": candidate_version}, justification="The notification candidate version is stale or missing.")
    if notification_type == "knowledge_capture_question":
        if question_id is None:
            _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_missing_question", severity=finding_severity, justification="The question notification has no question binding.")
        elif question is None:
            _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_missing_question", severity=finding_severity, details={"question_id": question_id}, justification="The question notification has no matching question.")
        else:
            if _uuid_text(_attr(question, "candidate_id")) != _uuid_text(_attr(candidate, "id")):
                _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_question_candidate_mismatch", severity=finding_severity, justification="The question notification points to another candidate.")
            if _uuid_text(_attr(question, "project_id")) != _uuid_text(_attr(candidate, "project_id")):
                _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_question_project_mismatch", severity=finding_severity, justification="The question notification question project is stale.")
            if _status(_attr(question, "status")) != "pending" and status not in TERMINAL_NOTIFICATION_STATES:
                _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_question_not_pending", severity="actionable", justification="The question notification no longer has a pending question.")
            q_version = _attr(question, "candidate_version")
            if q_version is None or _int_value(q_version) != candidate_version:
                _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_question_candidate_version_mismatch", severity=finding_severity, justification="The question notification version fence is stale.")
            if _attr(candidate, "evidence_digest") and _attr(question, "evidence_digest") and str(_attr(candidate, "evidence_digest")) != str(_attr(question, "evidence_digest")):
                _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_question_evidence_mismatch", severity=finding_severity, justification="The question notification references mismatched evidence.")
        if _status(_attr(candidate, "status")) != "needs_user" and status not in TERMINAL_NOTIFICATION_STATES:
            _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_question_candidate_not_needs_user", severity="actionable", justification="A question notification requires a needs_user candidate.")
        elif question is not None and _question_surface_allowed(candidate, task, question):
            _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_retained_high_interruption", severity="retain_review", justification="The question notification meets the exceptional interruption threshold.")
        elif status not in TERMINAL_NOTIFICATION_STATES:
            _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_below_interruption_policy", severity="actionable", justification="The question notification is not proven worth interrupting the user for.")
    elif notification_type == "knowledge_capture_draft":
        if question_id is not None:
            _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_draft_has_question", severity=finding_severity, justification="A draft notification must not carry a question binding.")
        if _status(_attr(candidate, "status")) != "draft_ready" and status not in TERMINAL_NOTIFICATION_STATES:
            _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_draft_candidate_not_ready", severity="actionable", justification="A draft notification requires a draft_ready candidate.")
        elif _candidate_surface_allowed(candidate, task):
            _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_retained_high_interruption", severity="retain_review", justification="The draft notification meets the surfaced reuse threshold.")
        elif status not in TERMINAL_NOTIFICATION_STATES:
            _append_finding(findings, entity_type="notification", entity_id=entity_id, code="notification_below_interruption_policy", severity="actionable", justification="The draft notification is not proven worth interrupting the user for.")
    _add_time_findings(findings, entity_type="notification", entity_id=entity_id, row=notification)


def _safe_discard_candidate(candidate: Any, *, task: Any | None) -> bool:
    """Prove only a structural orphan/mismatch discard, never semantic loss."""

    status = _status(_attr(candidate, "status"))
    if status in TERMINAL_CANDIDATE_STATES or status in {"draft_ready", "approved"}:
        return False
    if status not in KNOWN_CANDIDATE_STATES or bool(_attr(candidate, "user_edited")):
        return False
    if _attr(candidate, "published_node_id") or _attr(candidate, "target_node_id"):
        return False
    if _attr(candidate, "lease_owner") and _attr(candidate, "lease_expires_at") is not None:
        try:
            if _attr(candidate, "lease_expires_at") > datetime.utcnow():
                return False
        except TypeError:
            return False
    task_id = _candidate_ref(candidate)
    return task_id is None or task is None or _uuid_text(_attr(task, "project_id")) != _uuid_text(_attr(candidate, "project_id"))


def _append_link_cleanup_findings(*, candidate_safe: Mapping[str, bool], questions: Sequence[Any], notifications: Sequence[Any], findings: list[dict[str, Any]]) -> None:
    safe_ids = {candidate_id for candidate_id, safe in candidate_safe.items() if safe}
    for question in questions:
        if _status(_attr(question, "status")) != "pending":
            continue
        candidate_id = _uuid_text(_attr(question, "candidate_id"))
        if candidate_id in safe_ids:
            question_id = _uuid_text(_attr(question, "id")) or str(_attr(question, "id"))
            _append_finding(findings, entity_type="question", entity_id=question_id, code="question_linked_to_discard_candidate", severity="actionable", details={"candidate_id": candidate_id}, justification="A pending question linked to a safely discarded candidate must be dismissed first.")
    for notification in notifications:
        if (
            _status(_attr(notification, "channel")) != "in_app"
            or _status(_attr(notification, "status")) in TERMINAL_NOTIFICATION_STATES
        ):
            continue
        candidate_id, _, _ = _payload_ids(notification)
        if candidate_id in safe_ids:
            notification_id = _uuid_text(_attr(notification, "id")) or str(_attr(notification, "id"))
            _append_finding(findings, entity_type="notification", entity_id=notification_id, code="notification_linked_to_discard_candidate", severity="actionable", details={"candidate_id": candidate_id}, justification="A KC notification linked to a safely discarded candidate must be cancelled.")


def _audit_core(audit: Mapping[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(audit.get(key)) for key in ("schema_version", "version", "scope", "complete", "truncated", "counts", "candidates", "questions", "notifications", "non_kc_notifications", "findings", "records") if key in audit}


def audit_digest(audit: Mapping[str, Any] | Any) -> str:
    return stable_digest(_audit_core(_as_mapping(audit)))


def _entity_maps(audit: Mapping[str, Any]) -> dict[str, dict[str, Mapping[str, Any]]]:
    return {
        kind: {str(row.get("id")): row for row in audit.get(key, []) if isinstance(row, Mapping) and row.get("id")}
        for kind, key in (("candidate", "candidates"), ("question", "questions"), ("notification", "notifications"))
    }


def _finding_groups(audit: Mapping[str, Any]) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    result: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for finding in audit.get("findings", []):
        if isinstance(finding, Mapping):
            result[(str(finding.get("entity_type")), str(finding.get("entity_id")))].append(finding)
    return result


def _entity_snapshot(entity: Mapping[str, Any]) -> Mapping[str, Any]:
    snapshot = entity.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise PlanValidationError("audited entity has no immutable snapshot")
    return snapshot


def _bounded_reason(value: Any, *, field: str) -> str:
    text = _safe_text(value, limit=MAX_JUSTIFICATION_CHARS)
    if text is None:
        raise PlanValidationError(f"{field} is required and must be bounded/safe")
    if "\n" in text or "\r" in text:
        raise PlanValidationError(f"{field} must be one line")
    return text


def _canonical_action(action: Any) -> str:
    if not isinstance(action, Mapping):
        raise PlanValidationError("plan action must be an object")
    raw = str(action.get("action") or "").strip()
    return LEGACY_ACTION_ALIASES.get(raw, raw)


def _action_target(action: Mapping[str, Any], canonical_action: str | None = None) -> tuple[str, str]:
    target = action.get("target")
    if isinstance(target, Mapping):
        entity_type = str(target.get("type") or target.get("entity_type") or "").strip()
        target_id = target.get("id")
    else:
        entity_type = str(action.get("target_type") or "").strip()
        target_id = action.get("target_id")
    canonical = canonical_action or _canonical_action(action)
    if not entity_type:
        entity_type = {ACTION_DISMISS_QUESTION: "question", ACTION_CANCEL_NOTIFICATION: "notification", ACTION_DISCARD_CANDIDATE: "candidate"}.get(canonical, "")
    if target_id in (None, ""):
        legacy_name = {"candidate": "candidate_id", "question": "question_id", "notification": "notification_id"}.get(entity_type)
        target_id = action.get(legacy_name) if legacy_name else None
    target_text = _uuid_text(target_id)
    if entity_type not in ENTITY_TYPES or target_text is None:
        raise PlanValidationError("plan action target type/id is invalid")
    return entity_type, target_text


def _action_expected(action: Mapping[str, Any]) -> Mapping[str, Any]:
    expected = action.get("expected", action.get("expected_snapshot"))
    if not isinstance(expected, Mapping):
        raise PlanValidationError("every action requires an expected snapshot")
    return expected


def _plan_core(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema_version": plan.get("schema_version"), "version": plan.get("version"), "scope": copy.deepcopy(plan.get("scope")), "audit_sha256": str(plan.get("audit_sha256") or plan.get("audit_digest") or "").lower(), "actions": copy.deepcopy(plan.get("actions"))}


def plan_digest(plan: Mapping[str, Any] | Any) -> str:
    return stable_digest(_plan_core(_as_mapping(plan)))


def _postcondition_for(action: str, expected: Mapping[str, Any]) -> dict[str, Any]:
    if action == ACTION_DISMISS_QUESTION:
        return {"status": "dismissed", "version": _int_value(expected.get("version")) + 1}
    if action == ACTION_CANCEL_NOTIFICATION:
        return {"status": "cancelled"}
    if action == ACTION_DISCARD_CANDIDATE:
        return {"status": "discarded", "version": _int_value(expected.get("version")) + 1}
    return {"state": "unchanged"}


_DISMISS_FINDINGS = frozenset({"orphan_pending_question", "question_candidate_mismatch", "question_project_mismatch", "question_terminal_candidate", "question_missing_candidate_version", "question_candidate_version_mismatch", "question_evidence_digest_mismatch", "question_evidence_digest_missing", "question_candidate_not_needs_user", "question_below_interruption_policy", "question_linked_to_discard_candidate"})
_CANCEL_FINDINGS = frozenset({"malformed_knowledge_capture_payload", "notification_missing_candidate", "notification_candidate_project_mismatch", "terminal_candidate_has_active_notification", "notification_stale_candidate_version", "notification_missing_question", "notification_question_candidate_mismatch", "notification_question_project_mismatch", "notification_question_not_pending", "notification_question_candidate_version_mismatch", "notification_question_evidence_mismatch", "notification_question_candidate_not_needs_user", "notification_draft_has_question", "notification_draft_candidate_not_ready", "notification_below_interruption_policy", "notification_linked_to_discard_candidate"})
_DISCARD_CANDIDATE_FINDINGS = frozenset({
    "candidate_safe_orphan_discard",
    "candidate_below_interruption_policy",
    "candidate_personal_or_incidental",
    "candidate_non_operational_kind",
    "candidate_needs_user_without_pending_question",
})


def _action_dict(*, action: str, entity_type: str, entity_id: str, entity: Mapping[str, Any], findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    finding_codes = sorted({str(row.get("code")) for row in findings if row.get("code")})
    reasons = sorted({str(row.get("justification") or "") for row in findings}) if findings else ["No actionable finding; preserve the audited Knowledge Capture row."]
    reason = _bounded_reason("; ".join(reasons)[:MAX_JUSTIFICATION_CHARS], field="reason")
    expected = copy.deepcopy(dict(_entity_snapshot(entity)))
    return {"action": action, "target": {"type": entity_type, "id": entity_id}, "target_type": entity_type, "target_id": entity_id, "expected": expected, "postcondition": _postcondition_for(action, expected), "reason": reason, "justification": reason, "finding_codes": finding_codes, f"{entity_type}_id": entity_id}


def _validate_audit_shape(audit: Mapping[str, Any]) -> None:
    if int(audit.get("schema_version", -1)) != SCHEMA_VERSION or int(audit.get("version", -1)) != PLAN_VERSION:
        raise PlanValidationError("unsupported audit version")
    if audit.get("complete") is not True or audit.get("truncated") is not False:
        raise PlanValidationError("audit is not proven complete")
    counts = audit.get("counts")
    if not isinstance(counts, Mapping):
        raise PlanValidationError("audit counts are missing")
    for table in ("candidates", "questions", "notifications"):
        table_count = counts.get(table)
        rows = audit.get(table)
        if not isinstance(table_count, Mapping) or not isinstance(rows, list) or len(rows) != int(table_count.get("total", -1)):
            raise PlanValidationError(f"audit {table} rows are incomplete")
    non_kc = audit.get("non_kc_notifications")
    if (
        not isinstance(non_kc, Mapping)
        or int(non_kc.get("count", -1)) < 0
        or not _DIGEST_RE.fullmatch(str(non_kc.get("fingerprint") or ""))
    ):
        raise PlanValidationError("audit non-KC notification fingerprint is missing")


def build_plan(audit: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Build a deterministic frozen action for every audited entity."""

    audit_map = _as_mapping(audit)
    _validate_audit_shape(audit_map)
    actual_audit_sha = audit_digest(audit_map)
    if str(audit_map.get("audit_sha256") or "").lower() != actual_audit_sha:
        raise PlanValidationError("cannot build a plan from an invalid audit digest")
    entities = _entity_maps(audit_map)
    grouped = _finding_groups(audit_map)
    actions: list[dict[str, Any]] = []
    for entity_type in ("question", "notification", "candidate"):
        for entity_id, entity in sorted(entities[entity_type].items()):
            entity_findings = grouped.get((entity_type, entity_id), [])
            if not any(
                str(finding.get("severity")) == "actionable"
                for finding in entity_findings
            ):
                # A clean row, or one that is explicitly retained for review,
                # needs no mutation action.  Keeping the frozen plan limited
                # to actionable findings also makes the operator's decision
                # surface auditable without duplicating the entire database.
                continue
            codes = {str(row.get("code")) for row in entity_findings}
            state = _status(_entity_snapshot(entity).get("status"))
            if entity_type == "question" and state == "pending" and codes & _DISMISS_FINDINGS:
                action = ACTION_DISMISS_QUESTION
            elif entity_type == "notification" and state not in TERMINAL_NOTIFICATION_STATES and codes & _CANCEL_FINDINGS:
                action = ACTION_CANCEL_NOTIFICATION
            elif (
                entity_type == "candidate"
                and state not in TERMINAL_CANDIDATE_STATES
                and not bool(entity.get("user_edited"))
                and not entity.get("published_node_id")
                and not entity.get("target_node_id")
                and codes & _DISCARD_CANDIDATE_FINDINGS
            ):
                action = ACTION_DISCARD_CANDIDATE
            else:
                action = ACTION_KEEP
            actions.append(_action_dict(action=action, entity_type=entity_type, entity_id=entity_id, entity=entity, findings=entity_findings))
    plan: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "version": PLAN_VERSION, "scope": copy.deepcopy(audit_map.get("scope")), "audit_sha256": actual_audit_sha, "audit_digest": actual_audit_sha, "actions": actions}
    plan["plan_sha256"] = plan_digest(plan)
    return plan


def validate_plan(plan: Mapping[str, Any] | Any, audit: Mapping[str, Any] | Any | None = None) -> str:
    """Validate structure and, when supplied, exact membership/snapshots."""

    plan_map = _as_mapping(plan)
    if int(plan_map.get("schema_version", -1)) != SCHEMA_VERSION or int(plan_map.get("version", -1)) != PLAN_VERSION:
        raise PlanValidationError("unsupported plan version")
    audit_sha = str(plan_map.get("audit_sha256") or plan_map.get("audit_digest") or "").lower()
    if not _DIGEST_RE.fullmatch(audit_sha):
        raise PlanValidationError("plan audit_sha256 is invalid")
    actions = plan_map.get("actions")
    if not isinstance(actions, list):
        raise PlanValidationError("plan actions must be a list")
    actual_plan_sha = plan_digest(plan_map)
    if plan_map.get("plan_sha256") is not None and str(plan_map.get("plan_sha256")).lower() != actual_plan_sha:
        raise PlanValidationError("plan_sha256 does not match plan contents")
    entity_maps: dict[str, dict[str, Mapping[str, Any]]] = {kind: {} for kind in ENTITY_TYPES}
    finding_groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    audit_map: Mapping[str, Any] | None = None
    if audit is not None:
        audit_map = _as_mapping(audit)
        _validate_audit_shape(audit_map)
        actual_audit_sha = audit_digest(audit_map)
        if str(audit_map.get("audit_sha256") or "").lower() != actual_audit_sha or audit_sha != actual_audit_sha:
            raise PlanValidationError("plan audit digest does not match supplied audit")
        if plan_map.get("scope") != audit_map.get("scope"):
            raise PlanValidationError("plan scope does not match supplied audit")
        entity_maps = _entity_maps(audit_map)
        finding_groups = _finding_groups(audit_map)
    seen: set[tuple[str, str]] = set()
    actions_by_target: dict[tuple[str, str], str] = {}
    covered_findings: set[tuple[str, str]] = set()
    for index, raw_action in enumerate(actions):
        if not isinstance(raw_action, Mapping):
            raise PlanValidationError(f"actions[{index}] must be an object")
        action = _canonical_action(raw_action)
        if action not in ALLOWED_ACTIONS:
            raise PlanValidationError(f"actions[{index}] has unsupported action")
        entity_type, entity_id = _action_target(raw_action, action)
        if action == ACTION_DISMISS_QUESTION and entity_type != "question":
            raise PlanValidationError("dismiss_question must target a question")
        if action == ACTION_CANCEL_NOTIFICATION and entity_type != "notification":
            raise PlanValidationError("cancel_notification must target a notification")
        if action == ACTION_DISCARD_CANDIDATE and entity_type != "candidate":
            raise PlanValidationError("discard_candidate must target a candidate")
        key = (entity_type, entity_id)
        if key in seen:
            raise PlanValidationError(f"duplicate action target: {entity_type}/{entity_id}")
        seen.add(key)
        actions_by_target[key] = action
        expected = _action_expected(raw_action)
        _bounded_reason(raw_action.get("justification", raw_action.get("reason")), field=f"actions[{index}].justification")
        if audit_map is None:
            continue
        entity = entity_maps[entity_type].get(entity_id)
        if entity is None:
            raise PlanValidationError(f"action target is not present in audit: {entity_type}/{entity_id}")
        if dict(expected) != dict(_entity_snapshot(entity)):
            raise PlanValidationError(f"actions[{index}] expected snapshot does not match audit")
        codes = {str(row.get("code")) for row in finding_groups.get(key, [])}
        state = _status(expected.get("status"))
        if action == ACTION_DISMISS_QUESTION:
            if state != "pending" or not codes & _DISMISS_FINDINGS:
                raise PlanValidationError("dismiss_question is not a pending actionable audit target")
            candidate_id = _uuid_text(entity.get("candidate_id"))
            candidate = (
                entity_maps["candidate"].get(candidate_id)
                if candidate_id is not None
                else None
            )
            if (
                candidate is not None
                and _status(_entity_snapshot(candidate).get("status")) == "needs_user"
                and actions_by_target.get(("candidate", candidate_id))
                != ACTION_DISCARD_CANDIDATE
            ):
                raise PlanValidationError(
                    "dismiss_question requires a matching candidate discard when the candidate is needs_user"
                )
        elif action == ACTION_CANCEL_NOTIFICATION:
            if state in TERMINAL_NOTIFICATION_STATES or not codes & _CANCEL_FINDINGS:
                raise PlanValidationError("cancel_notification is not an active actionable audit target")
        elif action == ACTION_DISCARD_CANDIDATE:
            if (
                state in TERMINAL_CANDIDATE_STATES
                or bool(entity.get("user_edited"))
                or entity.get("published_node_id")
                or entity.get("target_node_id")
                or not codes & _DISCARD_CANDIDATE_FINDINGS
            ):
                raise PlanValidationError("discard_candidate is not proven safe by the audited policy findings")
        covered_findings.add(key)
    if audit_map is not None:
        actionable_keys = {
            key
            for key, rows in finding_groups.items()
            if any(str(row.get("severity")) == "actionable" for row in rows)
        }
        missing = actionable_keys - covered_findings
        if missing:
            raise PlanValidationError(f"plan omits audited finding target: {sorted(missing)}")
        for candidate_id in entity_maps["candidate"]:
            if actions_by_target.get(("candidate", candidate_id)) != ACTION_DISCARD_CANDIDATE:
                continue
            for question_id, question in entity_maps["question"].items():
                if str(question.get("candidate_id")) == candidate_id and _status(_entity_snapshot(question).get("status")) == "pending" and actions_by_target.get(("question", question_id)) != ACTION_DISMISS_QUESTION:
                    raise PlanValidationError("discard_candidate requires dismissal of each linked pending question")
            for notification_id, notification in entity_maps["notification"].items():
                if (
                    str(notification.get("candidate_id")) == candidate_id
                    and _status(_entity_snapshot(notification).get("channel"))
                    == "in_app"
                    and _status(_entity_snapshot(notification).get("status"))
                    not in TERMINAL_NOTIFICATION_STATES
                    and actions_by_target.get(("notification", notification_id))
                    != ACTION_CANCEL_NOTIFICATION
                ):
                    raise PlanValidationError("discard_candidate requires cancellation of each linked KC notification")
    return actual_plan_sha


async def audit_inbox(session: Any, *, options: AuditOptions | Mapping[str, Any] | None = None, limit: int | None = None, page_size: int | None = None, project_id: UUID | str | None = None) -> dict[str, Any]:
    """Count and fetch every scoped candidate, question, and KC notification."""

    audit_options = _coerce_options(options, limit=limit, page_size=page_size, project_id=project_id)
    candidate_counts = await _count_status_rows(session, KnowledgeCaptureCandidate, options=audit_options, label="candidate")
    question_counts = await _count_status_rows(session, KnowledgeCaptureQuestion, options=audit_options, label="question")
    notification_counts = await _count_kc_notifications(session, options=audit_options)
    non_kc_condition = ~NotificationDelivery.notification_type.in_(tuple(KC_TYPES))
    non_kc_counts = await _count_status_rows(
        session,
        NotificationDelivery,
        options=audit_options,
        label="non-KC notification",
        extra_conditions=(non_kc_condition,),
    )
    candidate_rows = await _fetch_all_pages(session, KnowledgeCaptureCandidate, conditions=_scope_condition(KnowledgeCaptureCandidate, audit_options), page_size=audit_options.page_size, expected_count=candidate_counts[1], label="candidate")
    question_rows = await _fetch_all_pages(session, KnowledgeCaptureQuestion, conditions=_scope_condition(KnowledgeCaptureQuestion, audit_options), page_size=audit_options.page_size, expected_count=question_counts[1], label="question")
    notification_conditions = [NotificationDelivery.notification_type.in_(tuple(KC_TYPES)), *_scope_condition(NotificationDelivery, audit_options)]
    notification_rows = await _fetch_all_pages(session, NotificationDelivery, conditions=notification_conditions, page_size=audit_options.page_size, expected_count=notification_counts[1], label="KC notification")
    non_kc_rows = await _fetch_all_pages(
        session,
        NotificationDelivery,
        conditions=[non_kc_condition, *_scope_condition(NotificationDelivery, audit_options)],
        page_size=audit_options.page_size,
        expected_count=non_kc_counts[1],
        label="non-KC notification",
    )
    candidate_counts_after = await _count_status_rows(session, KnowledgeCaptureCandidate, options=audit_options, label="candidate")
    question_counts_after = await _count_status_rows(session, KnowledgeCaptureQuestion, options=audit_options, label="question")
    notification_counts_after = await _count_kc_notifications(session, options=audit_options)
    non_kc_counts_after = await _count_status_rows(
        session,
        NotificationDelivery,
        options=audit_options,
        label="non-KC notification",
        extra_conditions=(non_kc_condition,),
    )
    if candidate_counts_after != candidate_counts or question_counts_after != question_counts or notification_counts_after != notification_counts or non_kc_counts_after != non_kc_counts:
        raise AuditCompletenessError("one or more audited result sets changed during audit")

    candidate_map = _id_map(candidate_rows)
    question_map = _id_map(question_rows)
    related_candidate_ids = [_uuid_text(_attr(row, "candidate_id")) for row in question_rows]
    related_candidate_ids += [_payload_ids(row)[0] for row in notification_rows]
    related_candidates = _id_map(await _fetch_by_ids(session, KnowledgeCaptureCandidate, [value for value in related_candidate_ids if value and value not in candidate_map], page_size=audit_options.page_size))
    all_candidate_map = {**related_candidates, **candidate_map}
    task_ids = sorted({task_id for task_id in (_candidate_ref(row) for row in candidate_rows) if task_id})
    task_map = _id_map(await _fetch_by_ids(session, Task, task_ids, page_size=audit_options.page_size))

    questions_by_candidate: dict[str, list[Any]] = defaultdict(list)
    for row in question_rows:
        candidate_id = _uuid_text(_attr(row, "candidate_id"))
        if candidate_id:
            questions_by_candidate[candidate_id].append(row)
    notifications_by_candidate: dict[str, list[Any]] = defaultdict(list)
    for row in notification_rows:
        candidate_id, _, _ = _payload_ids(row)
        if candidate_id:
            notifications_by_candidate[candidate_id].append(row)

    findings: list[dict[str, Any]] = []
    safe_discard: dict[str, bool] = {}
    for candidate in candidate_rows:
        candidate_id = _uuid_text(_attr(candidate, "id")) or str(_attr(candidate, "id"))
        task = task_map.get(_candidate_ref(candidate)) if _candidate_ref(candidate) else None
        safe = _safe_discard_candidate(candidate, task=task)
        safe_discard[candidate_id] = safe
        _classify_candidate(candidate, task=task, questions=questions_by_candidate.get(candidate_id, ()), notifications=notifications_by_candidate.get(candidate_id, ()), findings=findings)
        if safe:
            _append_finding(findings, entity_type="candidate", entity_id=candidate_id, code="candidate_safe_orphan_discard", severity="actionable", details={"proof": "missing_or_mismatched_seed_task", "safe": True}, justification="The non-terminal candidate is unedited and structurally orphaned.")
    _append_link_cleanup_findings(candidate_safe=safe_discard, questions=question_rows, notifications=notification_rows, findings=findings)
    for question in question_rows:
        candidate = all_candidate_map.get(_uuid_text(_attr(question, "candidate_id")))
        task = task_map.get(_candidate_ref(candidate)) if candidate is not None and _candidate_ref(candidate) else None
        _classify_question(question, candidate=candidate, task=task, findings=findings)
    for notification in notification_rows:
        candidate_id, question_id, _ = _payload_ids(notification)
        candidate = all_candidate_map.get(candidate_id)
        question = question_map.get(question_id) if question_id else None
        task = task_map.get(_candidate_ref(candidate)) if candidate is not None and _candidate_ref(candidate) else None
        _classify_notification(notification, candidate=candidate, question=question, task=task, findings=findings)
    findings.sort(key=lambda row: (ENTITY_ORDER.get(str(row.get("entity_type")), 99), str(row.get("entity_id")), str(row.get("code")), _canonical_json(row.get("details") or {})))
    grouped = _finding_groups({"findings": findings})

    candidate_records: list[dict[str, Any]] = []
    for candidate in candidate_rows:
        candidate_id = _uuid_text(_attr(candidate, "id")) or str(_attr(candidate, "id"))
        task = task_map.get(_candidate_ref(candidate)) if _candidate_ref(candidate) else None
        record = _candidate_projection(candidate, task)
        record["safe_discard"] = bool(safe_discard.get(candidate_id))
        record["finding_codes"] = sorted(str(row.get("code")) for row in grouped.get(("candidate", candidate_id), []))
        record["snapshot"] = _candidate_snapshot(candidate, task)
        candidate_records.append(record)
    candidate_records.sort(key=lambda row: row["id"])
    question_records: list[dict[str, Any]] = []
    for question in question_rows:
        question_id = _uuid_text(_attr(question, "id")) or str(_attr(question, "id"))
        candidate = all_candidate_map.get(_uuid_text(_attr(question, "candidate_id")))
        record = _question_projection(question, candidate)
        record["finding_codes"] = sorted(str(row.get("code")) for row in grouped.get(("question", question_id), []))
        record["snapshot"] = _question_snapshot(question, candidate)
        question_records.append(record)
    question_records.sort(key=lambda row: row["id"])
    notification_records: list[dict[str, Any]] = []
    for notification in notification_rows:
        notification_id = _uuid_text(_attr(notification, "id")) or str(_attr(notification, "id"))
        candidate_id, question_id, _ = _payload_ids(notification)
        candidate = all_candidate_map.get(candidate_id)
        question = question_map.get(question_id) if question_id else None
        record = _notification_projection(notification, candidate, question)
        record["finding_codes"] = sorted(str(row.get("code")) for row in grouped.get(("notification", notification_id), []))
        record["snapshot"] = _notification_snapshot(notification, candidate, question)
        notification_records.append(record)
    notification_records.sort(key=lambda row: row["id"])

    non_kc_fingerprint = stable_digest(
        [_notification_snapshot(notification) for notification in non_kc_rows]
    )
    non_kc_summary = {
        "count": len(non_kc_rows),
        "by_status": non_kc_counts[0],
        "by_channel": _status_counts(non_kc_rows, "channel"),
        "fingerprint": non_kc_fingerprint,
    }

    counts: dict[str, Any] = {
        "candidates": {"total": candidate_counts[1], "by_status": candidate_counts[0], "fetched": len(candidate_records)},
        "questions": {"total": question_counts[1], "by_status": question_counts[0], "fetched": len(question_records)},
        "notifications": {"total": notification_counts[1], "by_status": notification_counts[0], "fetched": len(notification_records)},
        "candidate_total": candidate_counts[1],
        "candidate_by_status": candidate_counts[0],
        "question_total": question_counts[1],
        "question_by_status": question_counts[0],
        "notification_total": notification_counts[1],
        "notification_by_status": notification_counts[0],
        "non_kc_notification_total": non_kc_counts[1],
        "non_kc_notification_by_status": non_kc_counts[0],
    }
    records = [
        {"entity_type": kind, "entity_id": row["id"], "finding_codes": row.get("finding_codes", [])}
        for kind, rows in (("candidate", candidate_records), ("question", question_records), ("notification", notification_records))
        for row in rows
    ]
    records.sort(key=lambda row: (ENTITY_ORDER.get(row["entity_type"], 99), row["entity_id"]))
    audit: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "version": PLAN_VERSION, "scope": audit_options.to_dict(), "complete": True, "truncated": False, "counts": counts, "candidates": candidate_records, "questions": question_records, "notifications": notification_records, "non_kc_notifications": non_kc_summary, "findings": findings, "records": records}
    audit["audit_sha256"] = audit_digest(audit)
    return audit


# Compatibility names used by maintenance callers.
build_audit = audit_inbox
collect_audit = audit_inbox
load_audit = audit_inbox
_notification_payload_valid = is_knowledge_capture_notification


async def _session_get(session: Any, model: Any, row_id: UUID, *, lock: bool = False) -> Any | None:
    statement = select(model).where(model.id == row_id).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update()
    result = await _execute(session, statement)
    method = getattr(result, "scalar_one_or_none", None)
    if callable(method):
        return method()
    rows = _scalar_rows(result)
    return rows[0] if rows else None


async def _load_related_for_action(session: Any, *, audit: Mapping[str, Any], actions: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    entities = _entity_maps(audit)
    candidate_ids: set[str] = set()
    question_ids: set[str] = set()
    notification_ids: set[str] = set()
    task_ids: set[str] = set()
    for raw_action in actions:
        canonical = _canonical_action(raw_action)
        entity_type, entity_id = _action_target(raw_action, canonical)
        if entity_type == "candidate":
            candidate_ids.add(entity_id)
            entity = entities["candidate"].get(entity_id)
            if entity and _uuid_text(entity.get("seed_task_id")):
                task_ids.add(str(entity["seed_task_id"]))
        elif entity_type == "question":
            question_ids.add(entity_id)
            entity = entities["question"].get(entity_id)
            if entity and _uuid_text(entity.get("candidate_id")):
                candidate_ids.add(str(entity["candidate_id"]))
        else:
            notification_ids.add(entity_id)
            entity = entities["notification"].get(entity_id)
            if entity:
                candidate_id = _uuid_text(entity.get("candidate_id"))
                question_id = _uuid_text(entity.get("question_id"))
                if candidate_id:
                    candidate_ids.add(candidate_id)
                if question_id:
                    question_ids.add(question_id)
    locked: dict[str, dict[str, Any]] = {"candidate": {}, "question": {}, "notification": {}, "task": {}}
    for row_id in sorted(candidate_ids):
        row = await _session_get(session, KnowledgeCaptureCandidate, UUID(row_id), lock=True)
        if row is not None:
            locked["candidate"][row_id] = row
    for row_id in sorted(question_ids):
        row = await _session_get(session, KnowledgeCaptureQuestion, UUID(row_id), lock=True)
        if row is not None:
            locked["question"][row_id] = row
    for row_id in sorted(notification_ids):
        row = await _session_get(session, NotificationDelivery, UUID(row_id), lock=True)
        if row is not None:
            locked["notification"][row_id] = row
    for row_id in sorted(task_ids):
        row = await _session_get(session, Task, UUID(row_id), lock=True)
        if row is not None:
            locked["task"][row_id] = row
    return locked


def _current_snapshot(entity_type: str, row: Any, *, related_candidates: Mapping[str, Any], related_questions: Mapping[str, Any], related_tasks: Mapping[str, Any]) -> dict[str, Any]:
    if entity_type == "candidate":
        task_id = _candidate_ref(row)
        return _candidate_snapshot(row, related_tasks.get(task_id) if task_id else None)
    if entity_type == "question":
        candidate_id = _uuid_text(_attr(row, "candidate_id"))
        return _question_snapshot(row, related_candidates.get(candidate_id) if candidate_id else None)
    candidate_id, question_id, _ = _payload_ids(row)
    return _notification_snapshot(row, related_candidates.get(candidate_id) if candidate_id else None, related_questions.get(question_id) if question_id else None)


def _mutate_action(row: Any, action: Mapping[str, Any], *, now: datetime) -> bool:
    canonical = _canonical_action(action)
    if canonical == ACTION_KEEP:
        return False
    if canonical == ACTION_DISMISS_QUESTION:
        if _status(_attr(row, "status")) != "pending":
            raise ReconciliationConflict("question is no longer pending")
        row.status = "dismissed"
        row.dismissed_at = now
        row.version = _int_value(_attr(row, "version"), 1) + 1
        row.updated_at = now
        return True
    if canonical == ACTION_CANCEL_NOTIFICATION:
        if (
            not _typed_namespace_notification(row)
            or _status(_attr(row, "channel")) != "in_app"
        ):
            raise ReconciliationConflict(
                "refusing to mutate a non-KC or non-in_app notification"
            )
        if _status(_attr(row, "status")) in TERMINAL_NOTIFICATION_STATES:
            raise ReconciliationConflict("notification is already cancelled")
        row.status = "cancelled"
        row.delivered_at = now
        row.updated_at = now
        return True
    if canonical == ACTION_DISCARD_CANDIDATE:
        current_status = _status(_attr(row, "status"))
        if current_status in TERMINAL_CANDIDATE_STATES:
            raise ReconciliationConflict(f"refusing to rewrite terminal candidate: {current_status}")
        if _attr(row, "lease_owner") and _attr(row, "lease_expires_at") is not None:
            try:
                if _attr(row, "lease_expires_at") > now:
                    raise ReconciliationConflict("candidate has an active lease")
            except TypeError as exc:
                raise ReconciliationConflict("candidate lease timestamp is invalid") from exc
        try:
            validate_candidate_transition(str(_attr(row, "status", "")), "discarded")
        except ValueError as exc:
            raise ReconciliationConflict(f"candidate transition is not legal: {exc}") from exc
        row.status = "discarded"
        row.version = _int_value(_attr(row, "version"), 1) + 1
        row.lease_owner = None
        row.lease_token = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        row.next_retry_at = None
        row.last_error_code = "reconciled_orphan"
        row.last_error_message = None
        row.updated_at = now
        return True
    raise ReconciliationConflict(f"unsupported action at mutation time: {canonical}")


async def _invoke_loader(loader: Callable[..., Any], session: Any, options: AuditOptions) -> Mapping[str, Any]:
    try:
        signature = inspect.signature(loader)
    except (TypeError, ValueError):
        signature = None
    if signature is not None and not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        kwargs: dict[str, Any] = {}
        if "session" in signature.parameters:
            kwargs["session"] = session
        if "options" in signature.parameters:
            kwargs["options"] = options
        result = loader(**kwargs) if kwargs else loader(session)
    else:
        result = loader(session=session, options=options)
    return _as_mapping(await result if inspect.isawaitable(result) else result)


async def apply_plan(session: Any, plan: Mapping[str, Any] | Any, *, expected_plan_sha256: str | None = None, live_audit: Mapping[str, Any] | Any | None = None, audit: Mapping[str, Any] | Any | None = None, options: AuditOptions | Mapping[str, Any] | None = None, limit: int | None = None, page_size: int | None = None, project_id: UUID | str | None = None, audit_loader: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Re-audit, lock/re-read every target, then apply atomically."""

    plan_map = _as_mapping(plan)
    actual_plan_sha = validate_plan(plan_map)
    expected_plan = expected_plan_sha256 or plan_map.get("plan_sha256")
    if not isinstance(expected_plan, str) or expected_plan.strip().lower() != actual_plan_sha:
        raise ReconciliationConflict(f"plan digest mismatch: expected={expected_plan}, actual={actual_plan_sha}")
    plan_audit_sha = str(plan_map.get("audit_sha256") or plan_map.get("audit_digest") or "").lower()
    audit_options = _coerce_options(options, limit=limit, page_size=page_size, project_id=project_id)
    try:
        for supplied_audit in (live_audit, audit):
            if supplied_audit is None:
                continue
            supplied = _as_mapping(supplied_audit)
            supplied_sha = audit_digest(supplied)
            if str(supplied.get("audit_sha256") or "").lower() != supplied_sha or supplied_sha != plan_audit_sha:
                raise ReconciliationConflict("supplied live audit digest does not match the plan")
        fresh = await _invoke_loader(audit_loader, session, audit_options) if audit_loader is not None else await audit_inbox(session, options=audit_options)
        _validate_audit_shape(fresh)
        fresh_sha = audit_digest(fresh)
        if str(fresh.get("audit_sha256") or "").lower() != fresh_sha:
            raise ReconciliationConflict("fresh audit has an invalid digest")
        if fresh_sha != plan_audit_sha:
            raise ReconciliationConflict(f"live audit changed since plan creation: expected={plan_audit_sha}, actual={fresh_sha}")
        validate_plan(plan_map, fresh)
        locked = await _load_related_for_action(session, audit=fresh, actions=[action for action in plan_map.get("actions", []) if isinstance(action, Mapping)])
        for raw_action in plan_map.get("actions", []):
            if not isinstance(raw_action, Mapping):
                raise ReconciliationConflict("plan action is not an object")
            canonical = _canonical_action(raw_action)
            entity_type, entity_id = _action_target(raw_action, canonical)
            row = locked[entity_type].get(entity_id)
            if row is None:
                raise ReconciliationConflict(f"planned {entity_type} disappeared before lock validation: {entity_id}")
            current = _current_snapshot(entity_type, row, related_candidates=locked["candidate"], related_questions=locked["question"], related_tasks=locked["task"])
            if dict(current) != dict(_action_expected(raw_action)):
                raise ReconciliationConflict(f"planned {entity_type} drifted before mutation: {entity_id}")
            if canonical == ACTION_CANCEL_NOTIFICATION and (
                not _typed_namespace_notification(row)
                or _status(_attr(row, "channel")) != "in_app"
            ):
                raise ReconciliationConflict(
                    f"refusing non-KC or non-in_app notification target: {entity_id}"
                )
        now = datetime.utcnow()
        changed_targets: list[dict[str, str]] = []
        for raw_action in plan_map.get("actions", []):
            canonical = _canonical_action(raw_action)
            entity_type, entity_id = _action_target(raw_action, canonical)
            if _mutate_action(locked[entity_type][entity_id], raw_action, now=now):
                changed_targets.append({"type": entity_type, "id": entity_id, "action": canonical})
        flush = getattr(session, "flush", None)
        commit = getattr(session, "commit", None)
        if not callable(flush) or not callable(commit):
            raise ReconciliationError("session does not support transactional flush/commit")
        result = flush()
        if inspect.isawaitable(result):
            await result
        result = commit()
        if inspect.isawaitable(result):
            await result
        return {"status": "applied", "changed_actions": len(changed_targets), "changed_targets": changed_targets, "plan_sha256": actual_plan_sha, "audit_sha256": fresh_sha}
    except Exception:
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            result = rollback()
            if inspect.isawaitable(result):
                await result
        raise


async def load_session(db_manager: Any | None = None) -> Any:
    """Open an existing session factory without running migrations."""

    manager = db_manager or get_database_manager()
    factory = getattr(manager, "SessionLocal", None)
    if callable(factory):
        result = factory()
        return await result if inspect.isawaitable(result) else result
    getter = getattr(manager, "get_session", None)
    if callable(getter):
        result = getter()
        return await result if inspect.isawaitable(result) else result
    raise RuntimeError("database manager does not expose a session factory")


def _write_json(path: str | None, value: Any) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


async def run(args: argparse.Namespace | None = None, *, apply: bool = False, plan: Mapping[str, Any] | None = None, expected_plan_sha256: str | None = None, audit_output: str | None = None, plan_output: str | None = None, options: AuditOptions | Mapping[str, Any] | None = None, limit: int | None = None, project_id: UUID | str | None = None, db_manager: Any | None = None) -> dict[str, Any] | int:
    """Runtime/CLI wrapper; no plan argument means dry-run only."""

    if args is not None:
        plan_path = getattr(args, "plan_file", None)
        apply = bool(plan_path)
        expected_plan_sha256 = getattr(args, "expected_plan_sha256", None)
        audit_output = getattr(args, "audit_output", None)
        plan_output = getattr(args, "plan_output", None)
        limit = getattr(args, "limit", limit)
        project_id = getattr(args, "project_id", project_id)
        if plan_path:
            plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    session = await load_session(db_manager)
    try:
        audit_options = _coerce_options(options, limit=limit, project_id=project_id)
        first_audit = await audit_inbox(session, options=audit_options)
        _write_json(audit_output, first_audit)
        if not apply:
            generated_plan = build_plan(first_audit)
            _write_json(plan_output, generated_plan)
            result: dict[str, Any] = {"mode": "dry-run", "audit": first_audit, "plan": generated_plan}
            if args is not None:
                print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
                return 0
            return result
        if plan is None:
            raise PlanValidationError("--apply-plan is required for apply")
        applied = await apply_plan(session, plan, expected_plan_sha256=expected_plan_sha256, live_audit=first_audit, options=audit_options)
        after = await audit_inbox(session, options=audit_options)
        if (
            first_audit.get("non_kc_notifications")
            != after.get("non_kc_notifications")
        ):
            raise ReconciliationConflict(
                "unrelated notification namespace changed during reconciliation"
            )
        result = {"mode": "apply", "apply": applied, "after": after}
        if args is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
            return 0
        return result
    except Exception:
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            result = rollback()
            if inspect.isawaitable(result):
                await result
        raise
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=DEFAULT_PAGE_SIZE, help="ページサイズ（件数上限ではなく、全ページを取得）")
    parser.add_argument("--project-id", help="監査対象をProject UUIDで絞る")
    parser.add_argument("--audit-output", help="監査JSONの保存先")
    parser.add_argument("--plan-output", help="dry-runで生成したplan JSONの保存先")
    parser.add_argument("--apply-plan", "--plan", dest="plan_file", help="dry-runで生成したJSON planを明示適用する")
    parser.add_argument("--expected-plan-sha256", help="plan_sha256の明示確認値")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.limit < 1 or args.limit > MAX_PAGE_SIZE:
        raise SystemExit(f"--limit must be between 1 and {MAX_PAGE_SIZE}")
    if bool(args.plan_file) != bool(args.expected_plan_sha256):
        raise SystemExit("--apply-plan/--plan and --expected-plan-sha256 must be used together")
    return int(asyncio.run(run(args)))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ACTION_CANCEL_NOTIFICATION",
    "ACTION_DISCARD_CANDIDATE",
    "ACTION_DISMISS_QUESTION",
    "ACTION_KEEP",
    "ALLOWED_ACTIONS",
    "ALLOWED_USER_QUESTION_KINDS",
    "AuditIncompleteError",
    "AuditCompletenessError",
    "AuditOptions",
    "PlanValidationError",
    "PlanDriftError",
    "ReconciliationConflict",
    "ReconciliationError",
    "apply_plan",
    "audit_digest",
    "audit_inbox",
    "build_audit",
    "build_parser",
    "build_plan",
    "collect_audit",
    "load_audit",
    "load_session",
    "main",
    "plan_digest",
    "run",
    "stable_digest",
    "validate_plan",
    "_notification_payload_valid",
]
