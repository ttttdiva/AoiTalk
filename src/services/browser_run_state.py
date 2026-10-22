"""Run-local browser observations and replay prevention; no new action authority."""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from .browser_agent_models import BrowserAgentError


class ReplanNeeded(BrowserAgentError):
    """Raised only when the attempted step is definitely not dispatched."""


class BrowserRunState:
    def __init__(self):
        self.salt = uuid4().hex
        self.values: set[str] = set()
        self.performed: list[dict[str, Any]] = []
        self.targets: set[str] = set()
        self.recovery_states: set[str] = set()

    def redact(self, value):
        if isinstance(value, str):
            for secret in sorted(self.values, key=len, reverse=True):
                value = value.replace(secret, "[entered value]")
            return value
        if isinstance(value, dict):
            return {k: self.redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        return value

    def register(self, plan):
        self.values.update(s.value for s in plan.steps if s.action in {"type", "select"} and s.value)

    def digest(self, value):
        return hashlib.sha256((self.salt + json.dumps(value, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()

    def keys(self, step, candidate, url):
        semantic = {k: candidate.get(k) for k in ("tag", "type", "role", "label", "context", "heading", "href")}
        keys = {self.digest([step.action, step.value, url, candidate.get("frame_id"), semantic])}
        if candidate.get("stable_id"):
            keys.add(self.digest([step.action, step.value, candidate.get("document_id"), candidate["stable_id"]]))
        return keys

    def check(self, keys):
        if keys & self.targets:
            raise BrowserAgentError("browser_repeated_action_blocked")

    def record(self, step, candidate, keys, outcome):
        self.targets.update(keys)
        self.performed.append(self.redact({
            "action": step.action, "instruction": step.instruction,
            "target": {k: candidate[k] for k in ("label", "context", "heading") if k in candidate},
            "execution_status": outcome.get("execution_status", "accepted"),
            "value_source": "user_goal" if step.action in {"type", "select"} else None,
        }))

    def observation(self, observed, *, planning=False):
        result = {k: observed[k] for k in ("url", "title", "coverage") if k in observed}
        result["text"] = observed.get("text", "")[:2000]
        if planning:
            items = list(observed.get("candidates", {}).values())
            result["visible_controls"] = [
                {k: item[k] for k in ("tag", "role", "label", "heading", "context", "enabled") if k in item}
                for item in items[:40]
            ]
            result["planning_controls_omitted"] = max(0, len(items) - 40)
        return self.redact(result)
