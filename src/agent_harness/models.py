"""Shared domain models for the AoiTalk agent harness."""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from datetime import datetime
from pathlib import Path
import math
import re
from typing import Any, Awaitable, Callable, Optional


HarnessEventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True)
class WorkItem:
    """Normalized task/project work item consumed by the harness."""

    id: str
    identifier: str
    title: str
    description: str = ""
    state: str = "todo"
    priority: int | str | None = None
    project_id: str | None = None
    project_name: str | None = None
    url: str | None = None
    labels: list[str] = field(default_factory=list)
    blocked_by: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Common AgentWork runtime metadata.  These fields are deliberately
    # additive and optional so the legacy tracker/runner contract continues
    # to work while a durable ``AgentWorkItem`` is introduced by the common
    # coordinator.  The harness never treats any of these values as authority;
    # they are a routing/execution projection supplied by the coordinator.
    source_type: str = "task"
    source_id: str | None = None
    source_revision: str | None = None
    intent_key: str = "agent_harness"
    domain: str = "task"
    work_item_id: str | None = None
    agent_id: str | None = None
    agent_revision_id: str | None = None
    task_id: str | None = None
    persona_id: str | None = None
    app_id: str | None = None
    execution_adapter: str = "code_agent"
    required_capabilities: list[str] = field(default_factory=list)
    concurrency_key: str | None = None
    root_work_item_id: str | None = None
    parent_work_item_id: str | None = None
    causation_id: str | None = None
    causal_depth: int = 0
    space_id: str | None = None
    team_id: str | None = None
    execution_profile_id: str | None = None
    subagent_id: str | None = None
    authority_hash: str | None = None
    run_scope_hash: str | None = None

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "identifier": self.identifier,
            "title": self.title,
            "description": self.description,
            "state": self.state,
            "priority": self.priority,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "space_id": self.space_id,
            "url": self.url,
            "labels": self.labels,
            "blocked_by": self.blocked_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "metadata": _safe_prompt_metadata(self.metadata),
            # Keep the prompt projection bounded and explicit.  In
            # particular, do not copy arbitrary coordinator payloads into the
            # rendered prompt; authority is resolved by the common runtime.
            "source_type": self.source_type,
            "source_id": self.source_id or self.id,
            "source_revision": self.source_revision,
            "intent_key": self.intent_key,
            "domain": self.domain,
            "work_item_id": self.work_item_id or self.id,
            "agent_id": self.agent_id,
            "agent_revision_id": self.agent_revision_id,
            "task_id": self.task_id,
            "persona_id": self.persona_id,
            "app_id": self.app_id,
            "execution_adapter": self.execution_adapter,
            "required_capabilities": list(self.required_capabilities),
            "concurrency_key": self.concurrency_key,
            "root_work_item_id": self.root_work_item_id,
            "parent_work_item_id": self.parent_work_item_id,
            "causation_id": self.causation_id,
            "causal_depth": self.causal_depth,
            "team_id": self.team_id,
            "execution_profile_id": self.execution_profile_id,
            "subagent_id": self.subagent_id,
        }


_PROMPT_SECRET_MARKERS = frozenset(
    {
        "secret",
        "token",
        "password",
        "credential",
        "authorization",
        "cookie",
        "api_key",
        "apikey",
        "private_key",
        "client_secret",
        "refresh_key",
        "access_key",
        "prompt",
        "transcript",
        "raw_response",
        "provider_response",
        "environment",
        "env",
        "path",
    }
)
_PROMPT_SENSITIVE_URL = re.compile(
    r"(?:https?|ftp)://[^\s]+(?:[?&](?:token|secret|password|key|sig|signature|credential)=|@[^\s/]+)",
    re.IGNORECASE,
)
_PROMPT_PATH = re.compile(
    r"^(?:[A-Za-z]:[\\/]|[\\/]{1,2}|(?:/|\\)(?:users?|home|tmp|var|etc|appdata)(?:[\\/]|$))",
    re.IGNORECASE,
)
_PROMPT_SECRET_VALUE = re.compile(
    r"\b(?:bearer|authorization|api[_-]?key|token|password)\s*[:=]?\s*[^\s]+",
    re.IGNORECASE,
)


def _safe_prompt_metadata(value: Any, *, depth: int = 0) -> Any:
    """Bound/redact arbitrary task metadata before workflow rendering."""

    if depth > 4:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        text = value[:2048]
        if (
            _PROMPT_SENSITIVE_URL.search(text)
            or _PROMPT_PATH.search(text)
            or _PROMPT_SECRET_VALUE.search(text)
        ):
            return "[REDACTED]"
        return text
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for raw_key, child in list(value.items())[:64]:
            key = str(raw_key)[:96]
            normalized = key.casefold().replace("-", "_")
            if any(marker in normalized for marker in _PROMPT_SECRET_MARKERS):
                continue
            projected[key] = _safe_prompt_metadata(child, depth=depth + 1)
        return projected
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_prompt_metadata(item, depth=depth + 1) for item in list(value)[:64]]
    return None


@dataclass
class RunResult:
    """Result returned by an agent runner attempt."""

    success: bool
    message: str = ""
    # Provider-native continuation/thread identifier.  This is deliberately
    # distinct from AoiTalk's durable conversation/session identifiers.
    provider_session_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    # Deprecated constructor/attribute alias for external runners migrating
    # from the pre-namespace contract.  It is an InitVar, so it does not
    # become a serialized/public model field; ``provider_session_id`` remains
    # the sole canonical value.
    session_id: InitVar[str | None] = None
    # Optional normalized classification used by the common ExecutionAdapter
    # seam.  Kept after the legacy InitVar so positional ``session_id``
    # construction remains backward compatible.
    classification: str | None = None

    def __post_init__(self, session_id: str | None) -> None:
        if self.provider_session_id is None and isinstance(session_id, str):
            self.provider_session_id = session_id.strip() or None

    @property
    def session_id(self) -> str | None:
        """Deprecated alias for ``provider_session_id``."""

        return self.provider_session_id

    @session_id.setter
    def session_id(self, value: str | None) -> None:
        self.provider_session_id = (
            value.strip() if isinstance(value, str) and value.strip() else None
        )


@dataclass
class RunningEntry:
    """In-memory state for one running work item."""

    work_item: WorkItem
    workspace_path: Path
    task: Any
    attempt: int | None
    started_at: datetime
    last_event: str | None = None
    last_message: Any = None
    last_event_at: datetime | None = None
    # Provider-native continuation/thread identifier observed while the run
    # is active.  Do not rename this back to ``session_id``: callers that need
    # an AoiTalk conversation session use their own domain models.
    provider_session_id: str | None = None
    codex_input_tokens: int = 0
    codex_output_tokens: int = 0
    codex_total_tokens: int = 0
    turn_count: int = 0
    # See RunResult.session_id.  Kept at the end so legacy positional
    # arguments for the pre-rename fields retain their ordering.
    session_id: InitVar[str | None] = None

    def __post_init__(self, session_id: str | None) -> None:
        if self.provider_session_id is None and isinstance(session_id, str):
            self.provider_session_id = session_id.strip() or None

    @property
    def session_id(self) -> str | None:
        """Deprecated alias for ``provider_session_id``."""

        return self.provider_session_id

    @session_id.setter
    def session_id(self, value: str | None) -> None:
        self.provider_session_id = (
            value.strip() if isinstance(value, str) and value.strip() else None
        )


@dataclass
class RetryEntry:
    """Scheduled retry state for one work item."""

    work_item: WorkItem
    attempt: int
    due_at: datetime
    error: str | None = None
    continuation: bool = False


@dataclass
class CodexTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    seconds_running: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "seconds_running": self.seconds_running,
        }
