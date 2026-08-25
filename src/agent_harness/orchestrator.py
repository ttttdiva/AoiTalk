"""Single-authority scheduler for work-item driven agent runs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from .config import AgentHarnessSettings
from .models import CodexTotals, RetryEntry, RunningEntry, WorkItem
from .runner import AgentRunner
from .tracker import WorkItemTracker
from .workflow import HarnessWorkflow, render_prompt
from .workspace import WorkspaceManager


class AgentHarnessOrchestrator:
    """Owns harness state, dispatch, retry, and reconciliation."""

    def __init__(
        self,
        *,
        settings: AgentHarnessSettings,
        tracker: WorkItemTracker,
        runner: AgentRunner,
        workspace_manager: WorkspaceManager,
        workflow: HarnessWorkflow,
    ):
        self.settings = settings
        self.tracker = tracker
        self.runner = runner
        self.workspace_manager = workspace_manager
        self.workflow = workflow
        self.running: dict[str, RunningEntry] = {}
        self.claimed: set[str] = set()
        self.retry_attempts: dict[str, RetryEntry] = {}
        self.completed: set[str] = set()
        self.codex_totals = CodexTotals()

    async def tick(self) -> dict[str, Any]:
        await self._collect_finished_runs()
        await self._reconcile_running()
        await self._dispatch_due_retries()
        await self._dispatch_candidates()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        now = datetime.utcnow()
        return {
            "enabled": self.settings.enabled,
            "running": [
                {
                    "work_item_id": item_id,
                    "identifier": entry.work_item.identifier,
                    "state": entry.work_item.state,
                    "attempt": entry.attempt,
                    "workspace_path": str(entry.workspace_path),
                    "provider_session_id": entry.provider_session_id,
                    "turn_count": entry.turn_count,
                    "last_event": entry.last_event,
                    "last_message": entry.last_message,
                    "runtime_seconds": max(0, int((now - entry.started_at).total_seconds())),
                    "codex_input_tokens": entry.codex_input_tokens,
                    "codex_output_tokens": entry.codex_output_tokens,
                    "codex_total_tokens": entry.codex_total_tokens,
                }
                for item_id, entry in sorted(self.running.items())
            ],
            "retrying": [
                {
                    "work_item_id": item_id,
                    "identifier": retry.work_item.identifier,
                    "attempt": retry.attempt,
                    "due_in_ms": max(0, int((retry.due_at - now).total_seconds() * 1000)),
                    "error": retry.error,
                    "continuation": retry.continuation,
                }
                for item_id, retry in sorted(self.retry_attempts.items())
            ],
            "claimed": sorted(self.claimed),
            "completed": sorted(self.completed),
            "codex_totals": self.codex_totals.to_dict(),
        }

    def run_detail(self, identifier_or_id: str) -> dict[str, Any] | None:
        for item_id, entry in self.running.items():
            if identifier_or_id in {item_id, entry.work_item.identifier}:
                return {"status": "running", **self._entry_detail(item_id, entry)}
        for item_id, retry in self.retry_attempts.items():
            if identifier_or_id in {item_id, retry.work_item.identifier}:
                return {
                    "status": "retrying",
                    "work_item_id": item_id,
                    "identifier": retry.work_item.identifier,
                    "attempt": retry.attempt,
                    "error": retry.error,
                }
        return None

    async def _collect_finished_runs(self) -> None:
        finished = [
            (item_id, entry)
            for item_id, entry in self.running.items()
            if entry.task.done()
        ]
        for item_id, entry in finished:
            self.running.pop(item_id, None)
            await self._handle_finished_run(item_id, entry)

    async def _handle_finished_run(self, item_id: str, entry: RunningEntry) -> None:
        runtime_seconds = max(0, int((datetime.utcnow() - entry.started_at).total_seconds()))
        self.codex_totals.seconds_running += runtime_seconds
        try:
            result = entry.task.result()
        except asyncio.CancelledError:
            self._schedule_retry(entry.work_item, self._next_attempt(entry.attempt), "run cancelled")
            return
        except Exception as exc:
            self._schedule_retry(entry.work_item, self._next_attempt(entry.attempt), str(exc))
            return

        self.codex_totals.input_tokens += max(0, result.input_tokens + entry.codex_input_tokens)
        self.codex_totals.output_tokens += max(0, result.output_tokens + entry.codex_output_tokens)
        self.codex_totals.total_tokens += max(0, result.total_tokens + entry.codex_total_tokens)
        # Some runners only learn the provider continuation handle in their
        # final result (rather than in a progress event).  Capture it before
        # the entry is removed so the in-memory state remains internally
        # consistent and custom runners can migrate independently.
        result_provider_session_id = _result_provider_session_id(result)
        if result_provider_session_id:
            self._set_provider_session_id(entry, result_provider_session_id)
        if result.success:
            self.completed.add(item_id)
            self._release_claim(item_id)
        else:
            self._schedule_retry(entry.work_item, self._next_attempt(entry.attempt), result.message)

    async def _reconcile_running(self) -> None:
        self._reconcile_stalled_runs()
        if not self.running:
            return
        refreshed = await self.tracker.fetch_by_ids(list(self.running.keys()))
        by_id = {item.id: item for item in refreshed}
        for item_id, entry in list(self.running.items()):
            current = by_id.get(item_id)
            if current is None:
                await self._stop_running(item_id, cleanup_workspace=False)
                continue
            if self._is_terminal(current.state):
                await self._stop_running(item_id, cleanup_workspace=True)
            elif not self._is_active(current.state):
                await self._stop_running(item_id, cleanup_workspace=False)
            else:
                entry.work_item = current

    def _reconcile_stalled_runs(self) -> None:
        timeout_ms = self.settings.codex.stall_timeout_ms
        if timeout_ms <= 0:
            return
        now = datetime.utcnow()
        for item_id, entry in list(self.running.items()):
            last = entry.last_event_at or entry.started_at
            elapsed_ms = int((now - last).total_seconds() * 1000)
            if elapsed_ms > timeout_ms:
                entry.task.cancel()
                self.running.pop(item_id, None)
                self._schedule_retry(
                    entry.work_item,
                    self._next_attempt(entry.attempt),
                    f"stalled for {elapsed_ms}ms",
                )

    async def _stop_running(self, item_id: str, *, cleanup_workspace: bool) -> None:
        entry = self.running.pop(item_id, None)
        if entry is None:
            self._release_claim(item_id)
            return
        entry.task.cancel()
        if cleanup_workspace:
            self.workspace_manager.remove_for(entry.work_item.identifier)
        self._release_claim(item_id)

    async def _dispatch_due_retries(self) -> None:
        now = datetime.utcnow()
        due = [
            (item_id, retry)
            for item_id, retry in self.retry_attempts.items()
            if retry.due_at <= now
        ]
        for item_id, retry in sorted(due, key=lambda item: item[1].due_at):
            if not self._slots_available_for(retry.work_item):
                self._schedule_retry(
                    retry.work_item,
                    retry.attempt + 1,
                    "no available harness slots",
                )
                continue
            self.retry_attempts.pop(item_id, None)
            if self._is_active(retry.work_item.state):
                await self._dispatch(retry.work_item, attempt=retry.attempt)
            else:
                self._release_claim(item_id)

    async def _dispatch_candidates(self) -> None:
        candidates = await self.tracker.fetch_candidates()
        for item in sort_work_items_for_dispatch(candidates):
            if not self._slots_available_for(item):
                break
            if self._should_dispatch(item):
                await self._dispatch(item, attempt=1)

    async def _dispatch(self, item: WorkItem, *, attempt: int | None) -> None:
        attempt_number = attempt or 1
        try:
            workspace, _created = self.workspace_manager.create_for(item.identifier)
            self.workspace_manager.run_before_run(workspace)
            prompt = render_prompt(self.workflow, issue=item, attempt=attempt_number)
        except Exception as exc:
            self._schedule_retry(item, attempt_number, str(exc))
            return

        async def on_event(event: dict[str, Any]) -> None:
            entry = self.running.get(item.id)
            if entry is None:
                return
            entry.last_event = str(event.get("event") or "")
            entry.last_message = event.get("message")
            entry.last_event_at = datetime.utcnow()
            provider_session_id = _event_provider_session_id(event)
            if provider_session_id:
                self._set_provider_session_id(entry, provider_session_id)
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            entry.codex_input_tokens += max(0, int(usage.get("input_tokens") or 0))
            entry.codex_output_tokens += max(0, int(usage.get("output_tokens") or 0))
            entry.codex_total_tokens += max(0, int(usage.get("total_tokens") or 0))

        async def run_attempt():
            try:
                return await self.runner.run(
                    work_item=item,
                    workspace=workspace,
                    prompt=prompt,
                    attempt=attempt_number,
                    on_event=on_event,
                )
            finally:
                self.workspace_manager.run_after_run(workspace)

        task = asyncio.create_task(run_attempt())
        self.running[item.id] = RunningEntry(
            work_item=item,
            workspace_path=workspace,
            task=task,
            attempt=attempt_number,
            started_at=datetime.utcnow(),
        )
        self.claimed.add(item.id)
        self.retry_attempts.pop(item.id, None)

    def _should_dispatch(self, item: WorkItem) -> bool:
        return (
            self._is_active(item.state)
            and not self._is_terminal(item.state)
            and item.id not in self.completed
            and item.id not in self.claimed
            and item.id not in self.running
            and not self._blocked_by_non_terminal(item)
            and self._slots_available_for(item)
        )

    def _slots_available_for(self, item: WorkItem) -> bool:
        if len(self.running) >= self.settings.max_concurrent_agents:
            return False
        state_limit = self.settings.max_concurrent_agents_by_state.get(
            _normalize_state(item.state),
            self.settings.max_concurrent_agents,
        )
        state_running = sum(
            1
            for entry in self.running.values()
            if _normalize_state(entry.work_item.state) == _normalize_state(item.state)
        )
        return state_running < state_limit

    def _schedule_retry(
        self,
        item: WorkItem,
        attempt: int,
        error: str | None,
        *,
        continuation: bool = False,
        delay_ms: int | None = None,
    ) -> None:
        if delay_ms is None:
            delay_ms = min(
                self.settings.failure_retry_base_ms * (2 ** max(0, attempt - 1)),
                self.settings.max_retry_backoff_ms,
            )
        self.retry_attempts[item.id] = RetryEntry(
            work_item=item,
            attempt=attempt,
            due_at=datetime.utcnow() + timedelta(milliseconds=delay_ms),
            error=error,
            continuation=continuation,
        )
        self.claimed.add(item.id)

    def _release_claim(self, item_id: str) -> None:
        self.claimed.discard(item_id)
        self.retry_attempts.pop(item_id, None)

    def _is_active(self, state: str) -> bool:
        return _normalize_state(state) in {
            _normalize_state(value) for value in self.settings.tracker.active_states
        }

    def _is_terminal(self, state: str) -> bool:
        return _normalize_state(state) in {
            _normalize_state(value) for value in self.settings.tracker.terminal_states
        }

    def _blocked_by_non_terminal(self, item: WorkItem) -> bool:
        if _normalize_state(item.state) != "todo":
            return False
        return any(
            not self._is_terminal(str(blocker.get("state") or ""))
            for blocker in item.blocked_by
        )

    def _next_attempt(self, attempt: int | None) -> int:
        return 1 if attempt is None else attempt + 1

    def _entry_detail(self, item_id: str, entry: RunningEntry) -> dict[str, Any]:
        return {
            "work_item_id": item_id,
            "identifier": entry.work_item.identifier,
            "workspace_path": str(entry.workspace_path),
            "last_event": entry.last_event,
            "provider_session_id": entry.provider_session_id,
        }

    @staticmethod
    def _set_provider_session_id(entry: RunningEntry, provider_session_id: str) -> None:
        """Update a running entry's provider handle and turn counter."""

        normalized = str(provider_session_id or "").strip()
        if not normalized:
            return
        if entry.provider_session_id != normalized:
            entry.turn_count += 1
            entry.provider_session_id = normalized


def sort_work_items_for_dispatch(items: list[WorkItem]) -> list[WorkItem]:
    return sorted(
        items,
        key=lambda item: (
            _priority_rank(item.priority),
            item.created_at or datetime.max,
            item.identifier,
        ),
    )


def _priority_rank(priority: Any) -> int:
    if isinstance(priority, int):
        return priority
    return {
        "urgent": 1,
        "high": 2,
        "medium": 3,
        "normal": 3,
        "low": 4,
    }.get(str(priority or "").strip().lower(), 5)


def _normalize_state(state: str) -> str:
    return str(state or "").strip().lower()


def _event_provider_session_id(event: dict[str, Any]) -> str | None:
    """Read the normalized provider handle from a runner event.

    ``provider_session_id`` is the public event contract.  Accepting a
    legacy top-level ``session_id`` here keeps existing custom runners
    working during migration without leaking that spelling through
    snapshots/details.
    """

    for key in ("provider_session_id", "session_id"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _result_provider_session_id(result: Any) -> str | None:
    """Read a provider handle from new or legacy runner result objects."""

    value = getattr(result, "provider_session_id", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    # A third-party runner may still return an object from the pre-rename
    # contract.  Keep this fallback private to the orchestrator; all public
    # state uses ``provider_session_id``.
    value = getattr(result, "session_id", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
