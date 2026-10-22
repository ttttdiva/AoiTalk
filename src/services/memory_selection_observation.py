"""Bounded observations of existing memory selection; never another retrieval.

Live entries are short-lived and contain only eight IDs/versions and actual
selection values. The same bounded record can accompany a persisted answer.
Labels/bodies are resolved through current memory ACL only at read time.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import math
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
PIPELINE = "automatic_context_memory"
EVENT = "memory_selection_progress"
METADATA_KEY = "memory_selection_observation"
MAX_ITEMS = 8
_LIVE: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
_LOCK = threading.Lock()
_STAGES = {"scope_selection", "relevance_decision", "context_budget"}
_STATUSES = {"running", "completed", "disabled", "skipped", "blocked", "failed", "cancelled"}


def decision_status(meta: dict[str, Any]) -> str:
    if meta.get("engine") == "jev":
        return "completed"
    reason = meta.get("reason")
    if reason == "disabled":
        return "disabled"
    if reason in {"not_applicable", "local_only", "no_egress_candidates"}:
        return "skipped"
    if reason in {"ExternalProviderBlocked", "PrivacyError", "external_provider_blocked", "privacy_blocked"}:
        return "blocked"
    return "failed"


def _number(value: Any) -> float | None:
    if type(value) not in (float, int) or not math.isfinite(value):
        return None
    return float(value)


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def bounded_candidates(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    rows = [row for row in rows[:10000] if isinstance(row, dict)]
    selected = [row for row in rows if row.get("selected") is True]
    selected_ids = {row.get("memory_id") for row in selected[:MAX_ITEMS]}
    chosen = selected[:MAX_ITEMS] + [row for row in rows if row.get("memory_id") not in selected_ids][:max(0, MAX_ITEMS - len(selected))]
    result = []
    seen = set()
    for row in chosen:
        identifier = _uuid(row.get("memory_id"))
        version = row.get("version")
        rank = row.get("candidate_rank")
        scope = row.get("scope")
        if (not identifier or identifier in seen or type(version) is not int or version < 1
                or type(rank) is not int or not 1 <= rank <= 10000
                or not isinstance(scope, str) or scope not in {"global", "user", "project", "task", "session"}):
            continue
        seen.add(identifier)
        item = {"memory_id": identifier, "version": version,
                "scope": scope, "candidate_rank": rank, "selected": row.get("selected") is True,
                "selection_reason": row.get("selection_reason") if isinstance(row.get("selection_reason"), str) and row.get("selection_reason") in {
                    "pinned", "keyword_and_scope_match", "semantic_match", "scope_priority", "context_budget_exceeded", "jev_low_relevance", "jev_not_evaluated"
                } else None}
        for key in ("weighted_score", "jev_relevance"):
            value = _number(row.get(key))
            if value is not None and 0 <= value <= 1:
                item[key] = value
        value = _number(row.get("semantic_score"))
        if value is not None and -1 <= value <= 1:
            item["semantic_score"] = value
        result.append(item)
    return sorted(result, key=lambda row: row["candidate_rank"])


def bounded_stage(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    stage, status, engine = payload.get("stage"), payload.get("status"), payload.get("engine")
    if (not all(isinstance(value, str) for value in (stage, status, engine))
            or stage not in _STAGES or status not in _STATUSES or engine not in {"host", "jev", "eg2"}):
        return None
    if stage == "relevance_decision":
        if status in {"running", "completed"} and engine != "jev":
            return None
    elif stage == "scope_selection" and engine not in {"host", "eg2"}:
        return None
    elif stage == "context_budget" and engine != "host":
        return None
    event = {"schema_version": SCHEMA_VERSION, "pipeline": PIPELINE,
             "stage": stage, "status": status, "engine": engine}
    reason = payload.get("reason")
    if isinstance(reason, str) and reason in {"disabled", "not_applicable", "context_memory_index_incomplete",
            "context_memory_index_unavailable", "context_memory_encoder_unavailable", "context_memory_search_failed", "context_memory_invalid_query_vector"}:
        event["reason"] = reason
    for key in ("candidate_count", "selected_count", "excluded_count"):
        value = payload.get(key)
        if type(value) is int and 0 <= value <= 10000:
            event[key] = value
    return event


class MemorySelectionCollector:
    def __init__(self, *, session_id: str, agent_run_id: str, client_message_id: str | None,
                 user_id: str, project_id: str | None, task_id: str | None, stream_callback: Any = None):
        self.record = {"schema_version": SCHEMA_VERSION, "pipeline": PIPELINE,
                       "binding": {"session_id": session_id, "agent_run_id": agent_run_id,
                                   "client_message_id": client_message_id, "user_id": user_id,
                                   "project_id": project_id, "task_id": task_id},
                       "stages": {}, "history": [], "candidates": []}
        self.stream_callback = stream_callback

    async def observe(self, payload: dict[str, Any]) -> None:
        event = bounded_stage(payload)
        if event is None:
            return
        # Raw provider exception text, query, candidate IDs and bodies are never
        # broadcast. The enum status distinguishes a skipped/failed Jev call.
        self.record["stages"][event["stage"]] = event
        self.record["history"] = [*self.record["history"], event][-8:]
        if isinstance(payload.get("candidates"), list):
            self.record["candidates"] = bounded_candidates(payload["candidates"])
        binding = self.record["binding"]
        with _LOCK:
            now = time.monotonic()
            for key in list(_LIVE):
                if now - _LIVE[key][0] > 600:
                    del _LIVE[key]
            _LIVE[binding["agent_run_id"]] = (now, self.export())
            _LIVE.move_to_end(binding["agent_run_id"])
            while len(_LIVE) > 128:
                _LIVE.popitem(last=False)
        if self.stream_callback:
            try:
                progress = {
                    **event, "session_id": binding["session_id"],
                    "agent_run_id": binding["agent_run_id"],
                    "client_message_id": binding["client_message_id"],
                }

                async def send():
                    result = self.stream_callback(EVENT, progress)
                    if inspect.isawaitable(result):
                        await result

                # Local context construction runs on a worker loop. Preserve
                # the callback's existing application loop for DB/WS work.
                target_loop = getattr(self.stream_callback, "loop", None)
                if target_loop is not None and target_loop is not asyncio.get_running_loop():
                    if target_loop.is_running():
                        future = asyncio.run_coroutine_threadsafe(send(), target_loop)
                        await asyncio.wait_for(asyncio.wrap_future(future), timeout=30)
                else:
                    await send()
            except Exception:
                # Observability must not fail the actual memory selection.
                pass

    def export(self) -> dict[str, Any]:
        return copy.deepcopy(self.record)


def collector_for_current_turn(*, session_id: str | None, user_id: str | None,
                               project_id: str | None, task_id: str | None,
                               stream_callback: Any = None) -> MemorySelectionCollector | None:
    from .agent_run_service import get_current_agent_run_id
    from .turn_context import get_turn_context

    turn = get_turn_context()
    run_id = get_current_agent_run_id()
    if (turn.suppress_automatic_context or not run_id or not turn.session_id or not turn.user_id
            or str(session_id or "") != str(turn.session_id)
            or str(user_id or "") != str(turn.user_id)):
        return None
    return MemorySelectionCollector(
        session_id=str(turn.session_id), agent_run_id=str(run_id),
        client_message_id=turn.client_message_id, user_id=str(turn.user_id),
        project_id=project_id, task_id=task_id, stream_callback=stream_callback,
    )


def observation_for_current_turn(record: Any) -> dict[str, Any] | None:
    from .agent_run_service import get_current_agent_run_id
    from .turn_context import get_turn_context

    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION or record.get("pipeline") != PIPELINE:
        return None
    binding = record.get("binding")
    turn = get_turn_context()
    run_id = get_current_agent_run_id()
    if (turn.suppress_automatic_context or not isinstance(binding, dict) or not turn.session_id or not turn.user_id or not run_id
            or binding.get("session_id") != str(turn.session_id)
            or binding.get("user_id") != str(turn.user_id)
            or binding.get("agent_run_id") != str(run_id)
            or binding.get("client_message_id") != turn.client_message_id):
        return None
    return copy.deepcopy(record)


def live_observation(session_id: str, agent_run_id: str) -> dict[str, Any] | None:
    with _LOCK:
        entry = _LIVE.get(agent_run_id)
        if not entry or time.monotonic() - entry[0] > 600:
            return None
        record = entry[1]
        if record.get("binding", {}).get("session_id") != session_id:
            return None
        return copy.deepcopy(record)


async def project_memory_selection(record: Any, *, actor_id: str, session_id: str,
                                   agent_run_id: str | None = None, memory_service: Any = None) -> dict[str, Any] | None:
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION or record.get("pipeline") != PIPELINE:
        return None
    binding = record.get("binding")
    if not isinstance(binding, dict) or binding.get("session_id") != session_id:
        return None
    if agent_run_id and binding.get("agent_run_id") != agent_run_id:
        return None
    # Automatic selection includes the sender's private memories. A participant
    # may read the chat while lacking authority to inspect that sender's trace.
    if not actor_id or str(binding.get("user_id") or "") != str(actor_id):
        return None
    if memory_service is None:
        from .scoped_memory_service import ScopedMemoryService
        memory_service = ScopedMemoryService()
    items = []
    for observation in bounded_candidates(record.get("candidates")):
        try:
            memory = await memory_service.get_memory(observation["memory_id"], actor_id=actor_id)
        except Exception:
            continue
        if memory.get("status") != "active" or memory.get("version") != observation["version"]:
            continue
        expiry = memory.get("expires_at")
        if expiry:
            try:
                expires = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires <= datetime.now(timezone.utc):
                    continue
            except (ValueError, TypeError):
                continue
        scope = memory.get("scope_type")
        if scope != observation["scope"]:
            continue
        # A changed scope must not become a different original under an old run.
        if scope in {"user", "global"} and str(memory.get("user_id")) != str(binding.get("user_id")):
            continue
        scope_key = {"project": "project_id", "task": "task_id", "session": "session_id"}.get(scope)
        if scope_key and str(memory.get(scope_key)) != str(binding.get(scope_key)):
            continue
        if scope not in {"global", "user", "project", "task", "session"}:
            continue
        items.append({**observation, "title": str(memory.get("title") or "記憶")[:180],
                      "excerpt": str(memory.get("content") or "")[:240], "scope": scope,
                      "updated_at": memory.get("updated_at"), "source": "scoped_memory"})
    raw_stages = record.get("stages")
    stages = {}
    if isinstance(raw_stages, dict):
        for key in _STAGES:
            row = bounded_stage(raw_stages.get(key))
            if row and row["stage"] == key:
                stages[key] = row
    raw_history = record.get("history")
    history = [event for row in raw_history[-8:] if (event := bounded_stage(row))] if isinstance(raw_history, list) else []
    return {"schema_version": SCHEMA_VERSION, "pipeline": PIPELINE, "stages": stages, "history": history, "items": items}
