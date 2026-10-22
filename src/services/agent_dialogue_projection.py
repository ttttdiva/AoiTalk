"""Safe projections for the AoiDots/AgentDialogue boundary.

The dialogue ledger contains references to ordinary Chat, AgentRun and
AgentWork rows.  It deliberately does not become a second conversation
history.  This module is the single bounded projection used by the service,
HTTP routes and runtime tools; message bodies, encrypted fields, lease tokens
and provider arguments never cross this boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from .agent_dialogue_budget import project_dialogue_budget


_MAX_LIST = 64
_MAX_MAP = 32
_MAX_TEXT = 512
_SAFE_REF_FIELDS = {
    "id",
    "dialogue_id",
    "root_session_id",
    "worker_session_id",
    "session_id",
    "source_message_id",
    "source_human_message_id",
    "source_run_id",
    "source_agent_run_id",
    "run_id",
    "work_item_id",
    "task_id",
    "project_id",
    "artifact_id",
    "artifact_ref",
    "chat_link",
    "chat_session_id",
    "input_id",
    "correlation_id",
    "receipt_id",
    "agent_id",
    "agent_revision_id",
    "worker_agent_id",
    "worker_agent_revision_id",
    "root_agent_id",
    "root_agent_revision_id",
    "current_run_id",
    "current_work_item_id",
    "current_delivery_id",
}


def _value(row: Any, *names: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        for name in names:
            if name in row:
                value = row[name]
                if value is not None:
                    return value
        return default
    for name in names:
        try:
            value = getattr(row, name)
        except Exception:
            continue
        if value is not None:
            return value
    return default


def _id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    text = str(value).strip()
    return text[:96] if text else None


def _text(value: Any, *, limit: int = _MAX_TEXT) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    """Return a small JSON projection without bodies or secret-shaped keys."""

    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_MAX_TEXT]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:_MAX_MAP]:
            key = str(raw_key).strip()
            lowered = key.casefold()
            if not key or any(
                marker in lowered
                for marker in (
                    "body",
                    "content",
                    "cipher",
                    "secret",
                    "token",
                    "password",
                    "credential",
                    "authorization",
                    "cookie",
                    "provider_args",
                    "tool_args",
                )
            ):
                continue
            projected = _safe_json(item, depth=depth + 1)
            if projected is not None:
                result[key[:96]] = projected
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_safe_json(item, depth=depth + 1) for item in list(value)[:_MAX_LIST]]
    return None


def safe_error(value: Any) -> str | None:
    """Project a stable error code/message without provider or body content."""

    if value in (None, ""):
        return None
    if isinstance(value, Mapping):
        candidate = value.get("code") or value.get("error_code") or value.get("safe_error_code")
        if candidate:
            return _text(candidate, limit=96)
        return None
    text = _text(value, limit=96)
    if not text:
        return None
    # The projection is consumed by UI and model context.  Avoid leaking a
    # provider response or an exception containing a request body.
    if any(marker in text.casefold() for marker in ("token", "password", "secret", "authorization", "cipher")):
        return "agent_dialogue_error"
    return text


def project_reference(value: Any) -> dict[str, Any]:
    """Project an artifact/delivery/reference row using only safe fields."""

    if isinstance(value, Mapping):
        source = value
    else:
        source = {
            name: getattr(value, name, None)
            for name in _SAFE_REF_FIELDS
            if hasattr(value, name)
        }
    result: dict[str, Any] = {}
    for key in _SAFE_REF_FIELDS:
        item = source.get(key)
        if item is None:
            continue
        if key.endswith("_at") or key in {"created_at", "updated_at", "deadline"}:
            result[key] = _time(item)
        elif key in {"status", "state", "kind", "type", "role", "name", "phase"}:
            result[key] = _text(item, limit=96)
        elif key.endswith("_ref") or key in {"chat_link", "artifact_ref"}:
            result[key] = _text(item, limit=256)
        else:
            result[key] = _id(item)
    # A mapping supplied by an internal adapter may carry the bounded safe
    # marker explicitly.  Never copy arbitrary mapping keys here.
    for key in ("status", "state", "kind", "type", "phase"):
        if key not in result and source.get(key) is not None:
            result[key] = _text(source[key], limit=96)
    return result


def project_input(value: Any) -> dict[str, Any]:
    """Return a typed question/reference projection without its body."""

    input_id = _id(_value(value, "id", "input_id"))
    payload = {
        "id": input_id,
        "dialogue_id": _id(_value(value, "dialogue_id")),
        "question_message_id": _text(
        _value(
            value,
            "reference_id",
            "question_ref",
            "message_id",
            "question_message_id",
        ),
        limit=256,
        ),
        "source_run_id": _id(_value(value, "source_run_id", "source_agent_run_id", "agent_run_id")),
        "state": _text(_value(value, "state", "status", default="pending"), limit=32) or "pending",
        "revision": int(_value(value, "revision", default=0) or 0),
        "expected_input_revision": max(1, int(_value(value, "revision", default=1) or 1)),
        "expected_control_revision": int(_value(value, "control_revision", "expected_control_revision", default=0) or 0),
        "answer_message_id": _id(_value(value, "answer_message_id")),
        "deadline": _time(_value(value, "deadline")),
    }
    return {key: item for key, item in payload.items() if item is not None}


def project_artifact(value: Any) -> dict[str, Any]:
    """Return only identity and lifecycle fields for an artifact."""

    payload = {
        "id": _id(_value(value, "id", "artifact_id")),
        "artifact_ref": _text(
        _value(value, "artifact_ref", "reference", "uri", "chat_link"),
        limit=256,
        ) or _id(_value(value, "id", "artifact_id")),
        "phase": _text(_value(value, "phase", "kind", "delivery_type"), limit=48),
        "delivery_id": _id(_value(value, "delivery_id")),
        "message_id": _id(_value(value, "message_id", "source_message_id", "receiver_message_id")),
        "created_at": _time(_value(value, "created_at")),
    }
    return {key: item for key, item in payload.items() if item is not None}


def _worker_lifecycle_status(
    base_status: str,
    *,
    worker_run: Any | None,
    worker_work: Any | None,
    worker_delivery: Any | None,
    pending_inputs: Sequence[Any],
    base_control_revision: int,
) -> tuple[str, str, str]:
    """Derive status from the current receiver evidence when supplied.

    ``run``/``work`` historically mean the immutable Human source anchor.  A
    worker Run/Work pair is a separate current execution identity and must win
    while it is active.  The source anchor remains available in ``refs``.
    """

    expected_control_revision = _value(worker_delivery, "expected_control_revision") if worker_delivery is not None else None
    has_worker_evidence = (
        worker_delivery is not None
        and expected_control_revision is not None
        and int(expected_control_revision or 0) == int(base_control_revision or 0)
    )
    if any(_text(_value(item, "state", "status", default="pending"), limit=24).casefold() == "pending" for item in pending_inputs):
        return "waiting", "worker" if has_worker_evidence else "dialogue", "waiting_for_input"
    if not has_worker_evidence:
        return base_status, "dialogue", base_status
    run_status = _text(_value(worker_run, "status", "state"), limit=32).casefold() if worker_run is not None else ""
    work_status = _text(_value(worker_work, "state", "status"), limit=32).casefold() if worker_work is not None else ""
    delivery_state = _text(_value(worker_delivery, "state", "status"), limit=32).casefold() if worker_delivery is not None else ""
    delivery_kind = (_text(_value(worker_delivery, "kind", "type"), limit=32) or "").casefold() if worker_delivery is not None else ""
    if work_status in {"blocked", "uncertain"} or run_status in {"blocked", "uncertain"}:
        return "blocked", "worker", "blocked"
    if delivery_kind in {"response", "event"} and (
        work_status in {"pending", "claimed", "running", "awaiting_approval"}
        or run_status in {"queued", "running", "awaiting_approval"}
    ):
        return "running", "worker", "receiving_result"
    if work_status in {"claimed", "running", "awaiting_approval"} or run_status in {"running", "awaiting_approval"}:
        return "running", "worker", "running"
    if work_status in {"retry_wait", "pending"} or run_status == "queued":
        return "queued", "worker", "queued"
    if work_status in {"failed", "dead_letter"} or run_status == "failed":
        if base_status in {"succeeded", "failed", "cancelled", "expired"}:
            return base_status, "dialogue", base_status
        return "failed", "worker", "failed"
    if work_status == "cancelled" or run_status == "cancelled":
        if base_status in {"succeeded", "failed", "cancelled", "expired"}:
            return base_status, "dialogue", base_status
        return "cancelled", "worker", "cancelled"
    if work_status == "succeeded" or run_status == "succeeded":
        if base_status in {"succeeded", "failed", "cancelled", "expired"}:
            return base_status, "dialogue", base_status
        if delivery_state in {"pending", "available", "leased", "delivered"}:
            return "running", "worker", "receiving_result"
        return "succeeded", "worker", "completed"
    return base_status, "worker", base_status


def project_dialogue(
    value: Any,
    *,
    pending_inputs: Sequence[Any] = (),
    artifacts: Sequence[Any] = (),
    run: Any | None = None,
    work: Any | None = None,
    worker_run: Any | None = None,
    worker_work: Any | None = None,
    worker_delivery: Any | None = None,
    current_run: Any | None = None,
    current_work: Any | None = None,
    current_delivery: Any | None = None,
    budget_owner: Any | None = None,
    root_agent_name: Any = None,
    worker_agent_name: Any = None,
) -> dict[str, Any]:
    """Build the canonical bounded AgentDialogue response."""

    dialogue_id = _id(_value(value, "id", "dialogue_id"))
    status = _text(_value(value, "status", "state", "job_status", default="queued"), limit=48) or "queued"
    control_revision = int(
        _value(value, "current_control_revision", "control_revision", default=0) or 0
    )
    worker_run = worker_run or current_run
    worker_work = worker_work or current_work
    worker_delivery = worker_delivery or current_delivery
    expected_control_revision = _value(worker_delivery, "expected_control_revision") if worker_delivery is not None else None
    try:
        worker_control_revision = int(expected_control_revision) if expected_control_revision is not None else None
    except (TypeError, ValueError):
        worker_control_revision = None
    if worker_delivery is None or worker_control_revision is None or worker_control_revision != control_revision:
        worker_run = None
        worker_work = None
        worker_delivery = None
    refs = {
        "root_session_id": _id(
            _value(value, "root_session_id", "root_conversation_session_id", "owner_session_id")
        ),
        "worker_session_id": _id(
            _value(value, "worker_session_id", "worker_conversation_session_id", "child_session_id")
        ),
        "source_message_id": _id(
            _value(
                value,
                "source_human_message_id",
                "source_message_id",
                "human_message_id",
                "trigger_message_id",
            )
        ),
        "source_human_message_id": _id(
            _value(
                value,
                "source_human_message_id",
                "source_message_id",
                "human_message_id",
                "trigger_message_id",
            )
        ),
        "source_run_id": _id(
            _value(
                value,
                "source_agent_run_id",
                "agent_run_id",
                "source_run_id",
                "human_run_id",
            )
        ),
        "source_human_run_id": _id(
            _value(
                value,
                "source_human_run_id",
                "source_agent_run_id",
                "agent_run_id",
                "source_run_id",
                "human_run_id",
            )
        ),
        "work_item_id": _id(
            _value(value, "work_item_id", "agent_work_item_id", "human_work_item_id")
        ),
        "source_human_work_item_id": _id(
            _value(
                value,
                "source_human_work_item_id",
                "work_item_id",
                "agent_work_item_id",
                "human_work_item_id",
            )
        ),
        "task_id": _id(_value(value, "task_id")),
        "project_id": _id(
            _value(value, "explicit_project_id", "project_id", "context_project_hint_id")
        ),
        "root_agent_id": _id(_value(value, "root_agent_id", "owner_agent_id")),
        "root_agent_revision_id": _id(
            _value(value, "root_agent_revision_id", "owner_agent_revision_id")
        ),
        "worker_agent_id": _id(_value(value, "worker_agent_id", "agent_id")),
        "worker_agent_revision_id": _id(
            _value(value, "worker_agent_revision_id", "agent_revision_id")
        ),
    }
    if run is not None:
        refs["source_run_id"] = refs["source_run_id"] or _id(_value(run, "id", "run_id"))
    if work is not None:
        refs["work_item_id"] = refs["work_item_id"] or _id(_value(work, "id", "work_item_id"))

    refs["current_run_id"] = _id(
        _value(worker_run, "id", "run_id") if worker_run is not None else _value(worker_work, "active_agent_run_id")
    )
    refs["current_work_item_id"] = _id(
        _value(worker_work, "id", "work_item_id") if worker_work is not None else _value(worker_run, "work_item_id")
    )
    refs["current_delivery_id"] = _id(_value(worker_delivery, "id", "delivery_id"))
    for key in ("current_run_id", "current_work_item_id", "current_delivery_id"):
        if refs.get(key) is None:
            refs.pop(key, None)

    input_items = [project_input(item) for item in list(pending_inputs)[:_MAX_LIST]]
    artifact_items = [project_artifact(item) for item in list(artifacts)[:_MAX_LIST]]
    input_items = [item for item in input_items if item.get("id")]
    artifact_items = [item for item in artifact_items if item.get("id") or item.get("artifact_ref")]

    status, status_source, phase = _worker_lifecycle_status(
        status,
        worker_run=worker_run,
        worker_work=worker_work,
        worker_delivery=worker_delivery,
        pending_inputs=input_items,
        base_control_revision=control_revision,
    )
    allowed_actions: list[str] = []
    if status in {"queued", "running", "waiting", "blocked"}:
        allowed_actions.append("stop")
    if status in {"waiting", "blocked"}:
        allowed_actions.append("resume")
    if status in {"succeeded", "failed", "cancelled", "expired", "blocked"}:
        allowed_actions.append("rework")
    if input_items:
        allowed_actions.append("answer")
    human_status = {
        "queued": "Queued",
        "running": "Running",
        "waiting": "Waiting for input",
        "blocked": "Blocked",
        "succeeded": "Completed",
        "failed": "Failed",
        "cancelled": "Stopped",
        "expired": "Expired",
    }.get(status, status)
    if phase == "receiving_result":
        human_status = "Receiving result"
    budget = None
    if any(hasattr(value, name) for name in ("max_attempts", "max_tokens", "max_cost_micros", "usage_state")):
        budget = project_dialogue_budget(value, budget_owner=budget_owner)
    safe = {
        "dialogue_id": dialogue_id,
        "job_status": status,
        "human_status": human_status,
        "root_agent_name": _text(root_agent_name, limit=160),
        "worker_agent_name": _text(worker_agent_name, limit=160),
        "status_source": status_source if status_source == "worker" else None,
        "phase": phase if status_source == "worker" else None,
        "current_run_status": _text(_value(worker_run, "status", "state"), limit=32) if worker_run is not None else None,
        "current_work_status": _text(_value(worker_work, "state", "status"), limit=32) if worker_work is not None else None,
        "current_delivery_state": _text(_value(worker_delivery, "state", "status"), limit=32) if worker_delivery is not None else None,
        "allowed_actions": list(dict.fromkeys(allowed_actions)),
        "refs": refs,
        "control_revision": control_revision,
        "pending_inputs": input_items,
        "artifacts": artifact_items,
        "safe_error": safe_error(
            _value(value, "safe_error_code", "safe_error", "error_code", "error")
        ),
        "deadline": _time(_value(value, "deadline")),
        "created_at": _time(_value(value, "created_at")),
        "updated_at": _time(_value(value, "updated_at")),
        "budget": budget,
    }
    return {key: item for key, item in safe.items() if item is not None}


def project_profile(
    *,
    root_id: Any = None,
    session_id: Any = None,
    agent_id: Any = None,
    agent_name: Any = None,
    state: Any = "ready",
    pending_input_count: int = 0,
) -> dict[str, Any]:
    """Build the stable AoiDots bootstrap DTO."""

    return {
        "root_id": _id(root_id),
        "session_id": _id(session_id),
        "agent_id": _id(agent_id),
        "agent_name": _text(agent_name, limit=160) or "AoiDots",
        "state": _text(state, limit=32) or "ready",
        "pending_input_count": max(0, min(int(pending_input_count or 0), _MAX_LIST)),
    }


def build_turn_context(value: Any, *, pending_inputs: Sequence[Any] = ()) -> dict[str, Any]:
    """Return a bounded server-derived context for a root/worker turn."""

    projection = project_dialogue(value, pending_inputs=pending_inputs)
    refs = projection.get("refs", {})
    return {
        "kind": "agent_dialogue",
        "dialogue_id": projection.get("dialogue_id"),
        "parent_root_session_id": refs.get("root_session_id"),
        "root_session_id": refs.get("root_session_id"),
        "worker_session_id": refs.get("worker_session_id"),
        "source_agent": {
            "agent_id": refs.get("worker_agent_id"),
            "agent_revision_id": refs.get("worker_agent_revision_id"),
        },
        "source_human": {
            "message_id": refs.get("source_message_id"),
            "run_id": refs.get("source_run_id"),
        },
        "scope": {
            "project_id": refs.get("project_id"),
            "task_id": refs.get("task_id"),
            "project_hint_only": bool(
                _value(value, "context_project_hint_id")
                and _value(value, "explicit_project_id") in (None, "")
            ),
        },
        "pending_inputs": projection.get("pending_inputs", [])[:_MAX_LIST],
        "constraints": {
            "human_approval": False,
            "agent_instruction_expands_authority": False,
            "cross_agent_parent_run": False,
            "projectless_root_memory": refs.get("project_id") in (None, ""),
        },
    }


__all__ = [
    "build_turn_context",
    "project_artifact",
    "project_dialogue",
    "project_input",
    "project_profile",
    "project_reference",
    "safe_error",
]
