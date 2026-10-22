"""Read executed tool results, never perform another search for the display."""

import json
from typing import Any

from .memory_selection_observation import MAX_ITEMS, _number, _uuid


def match_observed_original(hit: dict[str, Any], originals: list[Any]) -> Any:
    """Only a unique matching active row can carry this hit's observed score."""
    if len(originals) >= 100:
        return None  # The bounded query cannot prove uniqueness beyond its cap.
    text = hit.get("content") or hit.get("excerpt")
    if not isinstance(text, str) or not text:
        return None
    # The text tool adds this exact ellipsis at excerpt boundaries.
    fragment = text if hit.get("content") else text.strip("…")
    if not fragment:
        return None
    matched = [row for row in originals if fragment in str(row.content or "")]
    return matched[0] if len(matched) == 1 else None


async def project_memory_tool_results(calls: Any, *, actor_id: str, resolve_conversation: Any,
                                      memory_service: Any = None) -> dict[str, Any]:
    if memory_service is None:
        from .scoped_memory_service import ScopedMemoryService
        memory_service = ScopedMemoryService()
    summaries, items = [], []
    if not isinstance(calls, list):
        calls = []
    for call_index, call in enumerate(calls[:64]):
        if not isinstance(call, dict):
            continue
        tool = str(call.get("tool_name") or call.get("tool") or "").rsplit(".", 1)[-1]
        if tool not in {"search_past_chats", "semantic_memory_search"} or len(summaries) >= 4:
            continue
        raw = call.get("result", call.get("output"))
        payload = raw if isinstance(raw, dict) else None
        if isinstance(raw, str) and len(raw) <= 200000:
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                payload = None
        if not isinstance(payload, dict):
            payload = {}
        mode = payload.get("mode") if isinstance(payload.get("mode"), str) and payload.get("mode") in {"semantic", "text"} else "semantic"
        status = "failed" if payload.get("success") is False or call.get("success") is False or call.get("error") else "unavailable" if payload.get("success") is not True else "partial" if payload.get("partial") is True else "completed"
        call_key = str(call.get("id") or call.get("tool_call_id") or call_index)[:160]
        summaries.append({"call_id": call_key, "tool": tool, "mode": mode, "status": status})
        hits = payload.get("results")
        if not isinstance(hits, list) or status == "failed":
            continue
        for hit_index, hit in enumerate(hits[:MAX_ITEMS]):
            if not isinstance(hit, dict) or len(items) >= MAX_ITEMS:
                continue
            kind = hit.get("type")
            if kind == "dreaming_memory":
                identifier = _uuid(hit.get("id"))
                if not identifier:
                    continue
                try:
                    memory = await memory_service.get_memory(identifier, actor_id=actor_id)
                except Exception:
                    continue
                # This tool does not return a version. Its exact original body
                # and timestamp must still match before an old score is shown.
                if (memory.get("status") != "active" or memory.get("content") != hit.get("content")
                        or (memory.get("updated_at") or memory.get("created_at")) != hit.get("timestamp")):
                    continue
                from datetime import datetime, timezone
                expiry = memory.get("expires_at")
                if expiry:
                    try:
                        value = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                        if value.tzinfo is None:
                            value = value.replace(tzinfo=timezone.utc)
                        if value <= datetime.now(timezone.utc):
                            continue
                    except (ValueError, TypeError):
                        continue
                original = {"kind": "scoped_memory", "source_id": identifier, "version": memory.get("version"),
                            "title": str(memory.get("title") or "記憶原本")[:180],
                            "excerpt": str(memory.get("content") or "")[:240], "href": None}
                score_kind = "memory_relevance"
            else:
                identifier = _uuid(hit.get("session_id"))
                if not identifier:
                    continue
                original = await resolve_conversation(hit, identifier)
                if original is None:
                    continue
                score_kind = "semantic_relevance" if mode == "semantic" else "text_match"
            score = _number(hit.get("relevance_score"))
            # The original hit position and source identity survive ACL filtering.
            # A removed earlier original must never rebind its key to the next hit.
            items.append({**original, "ref_key": f"{call_key}:{hit_index}:{identifier}", "call_id": call_key,
                          "score": score, "score_kind": score_kind})
    return {"pipeline": "explicit_memory_search", "calls": summaries, "items": items}
