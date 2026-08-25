"""Shared domain models for the AoiTalk agent harness."""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from datetime import datetime
from pathlib import Path
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
            "url": self.url,
            "labels": self.labels,
            "blocked_by": self.blocked_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "metadata": self.metadata,
        }


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
