"""Single-authority scheduler for work-item driven agent runs."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import AgentHarnessSettings
from .models import CodexTotals, RetryEntry, RunningEntry, WorkItem
from .runner import AgentRunner
from .tracker import WorkItemTracker
from .workflow import HarnessWorkflow, render_prompt
from .workspace import WorkspaceManager
from ..features import Features


logger = logging.getLogger(__name__)


class AgentHarnessOrchestrator:
    """Owns harness state, dispatch, retry, and reconciliation."""

    def __init__(
        self,
        *,
        settings: AgentHarnessSettings,
        tracker: WorkItemTracker | None = None,
        runner: AgentRunner | None = None,
        workspace_manager: WorkspaceManager | None = None,
        workflow: HarnessWorkflow | None = None,
        # When supplied, the common durable runtime is the sole owner of
        # claim/lease/retry/concurrency/settlement.  The legacy in-memory
        # scheduler below remains available only when this is ``None`` so
        # existing manual harness callers do not change behavior.
        coordinator: Any | None = None,
        execution_adapter: Any | None = None,
        work_source: Any | None = None,
    ):
        self.settings = settings
        self.tracker = tracker
        self.runner = runner
        self.workspace_manager = workspace_manager
        self.workflow = workflow
        self.coordinator = coordinator
        self.execution_adapter = execution_adapter
        self.work_source = work_source
        self._common_state: dict[str, Any] = {}
        if self.coordinator is None and any(
            value is None
            for value in (self.tracker, self.runner, self.workspace_manager, self.workflow)
        ):
            raise ValueError(
                "tracker, runner, workspace_manager and workflow are required "
                "when no common AgentWork coordinator is supplied"
            )
        self.running: dict[str, RunningEntry] = {}
        self.claimed: set[str] = set()
        self.retry_attempts: dict[str, RetryEntry] = {}
        self.completed: set[str] = set()
        self.codex_totals = CodexTotals()
        # ``running`` entries stay published until the runner task and its
        # post-run hook have settled.  This set lets reconciliation skip an
        # entry while another lifecycle path is stopping it.  The lock keeps
        # concurrent stop requests from performing cleanup twice.
        self._stopping: set[str] = set()
        self._stop_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_started = False
        self._shutdown_complete = False

    @property
    def uses_common_runtime(self) -> bool:
        """Whether durable AgentWork coordination owns this facade."""

        return self.coordinator is not None

    @property
    def common_runtime(self) -> Any | None:
        """Return the bound coordinator for compatibility integrations."""

        return self.coordinator

    @property
    def common_coordinator(self) -> Any | None:
        """Alias used by API composition roots during migration."""

        return self.coordinator

    def bind_common_runtime(
        self,
        coordinator: Any,
        *,
        execution_adapter: Any | None = None,
        work_source: Any | None = None,
    ) -> Any:
        """Attach a durable coordinator to an existing compatibility facade.

        This is intentionally a synchronous wiring helper; registration on
        the coordinator is likewise expected to be synchronous.  The next
        ``tick`` still repeats registration defensively for coordinators that
        rebuild their registries after restart.
        """

        if coordinator is None:
            raise ValueError("coordinator is required")
        self.coordinator = coordinator
        if execution_adapter is not None:
            self.execution_adapter = execution_adapter
        if work_source is not None:
            self.work_source = work_source
        if bool(getattr(coordinator, "enabled", True)) and self.work_source is not None:
            register_source = getattr(coordinator, "register_source", None)
            if callable(register_source):
                try:
                    register_source(
                        self.work_source,
                        source_type=getattr(self.work_source, "source_type", None),
                    )
                except TypeError:
                    register_source(self.work_source)
        if bool(getattr(coordinator, "enabled", True)) and self.execution_adapter is not None:
            register_adapter = getattr(coordinator, "register_adapter", None)
            if callable(register_adapter):
                try:
                    register_adapter(
                        self.execution_adapter,
                        adapter_key=getattr(self.execution_adapter, "adapter_key", None),
                    )
                except TypeError:
                    register_adapter(self.execution_adapter)
        return coordinator

    async def tick(self) -> dict[str, Any]:
        if self.coordinator is not None:
            return await self._tick_common_runtime()
        if self._shutdown_started or not self.settings.enabled:
            return self.snapshot()
        await self._collect_finished_runs()
        await self._reconcile_running()
        if self._shutdown_started or not self.settings.enabled:
            return self.snapshot()
        await self._dispatch_due_retries()
        await self._dispatch_candidates()
        return self.snapshot()

    async def _tick_common_runtime(self) -> dict[str, Any]:
        """Forward one compatibility tick to ``AgentWorkCoordinator``.

        Coordinator implementations evolved during WS02, so this bridge
        accepts the repository's canonical ``execute_once``/``snapshot`` API
        as well as small embedding fakes exposing ``tick`` or ``run_once``.
        Crucially, it never falls back to the local ``claimed`` or retry sets
        when a coordinator is present.
        """

        if self._shutdown_started or not self.settings.enabled:
            return await self._common_snapshot()
        coordinator = self.coordinator
        if coordinator is None:  # pragma: no cover - guarded by caller
            return self.snapshot()
        method = None
        for name in ("tick", "execute_once", "run_once", "dispatch_due", "poll_once", "step"):
            candidate = getattr(coordinator, name, None)
            if callable(candidate):
                method = candidate
                break
        if method is None:
            return await self._common_snapshot()

        result = await _invoke_common_method(
            method,
            coordinator=coordinator,
            work_source=self.work_source,
            execution_adapter=self.execution_adapter,
            settings=self.settings,
        )
        if isinstance(result, dict):
            self._common_state = result
            return result
        return await self._common_snapshot(fallback=result)

    async def _common_snapshot(self, fallback: Any | None = None) -> dict[str, Any]:
        coordinator = self.coordinator
        snapshot = getattr(coordinator, "snapshot", None) if coordinator is not None else None
        if callable(snapshot):
            try:
                value = snapshot()
                if inspect.isawaitable(value):
                    value = await value
                if isinstance(value, dict):
                    self._common_state = value
                    return value
            except Exception:
                logger.exception("AgentWork coordinator snapshot failed")
        if isinstance(fallback, dict):
            self._common_state = fallback
            return fallback
        state = {
            "enabled": bool(getattr(coordinator, "enabled", False))
            if coordinator is not None
            else False,
            "common_runtime": True,
            "result": fallback,
        }
        self._common_state = state
        return state

    async def snapshot_async(self) -> dict[str, Any]:
        """Return the durable coordinator projection without sync leakage."""

        if self.coordinator is None:
            return self.snapshot()
        return await self._common_snapshot()

    def snapshot(self) -> dict[str, Any]:
        if self.coordinator is not None:
            # ``snapshot`` is intentionally synchronous for the historical
            # API.  If the durable coordinator exposes an async snapshot,
            # callers should use ``tick``/``state_async``; returning a small
            # projection here avoids leaking coroutine objects to JSON.
            snapshot = getattr(self.coordinator, "snapshot", None)
            if callable(snapshot):
                try:
                    value = snapshot()
                    if inspect.isawaitable(value):
                        # ``snapshot`` on the canonical coordinator is async;
                        # the old facade method is sync.  Close the coroutine
                        # instead of leaking an un-awaited warning.
                        close = getattr(value, "close", None)
                        if callable(close):
                            close()
                        value = None
                    if isinstance(value, dict):
                        self._common_state = value
                        return value
                except Exception:
                    logger.exception("AgentWork coordinator snapshot failed")
            if self._common_state:
                return dict(self._common_state)
            return {
                "enabled": bool(getattr(self.coordinator, "enabled", False)),
                "common_runtime": True,
                "running": [],
                "retrying": [],
                "claimed": [],
                "completed": [],
            }
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
        if self.coordinator is not None:
            for row in self._common_state.get("work_items", ()):
                if not isinstance(row, dict):
                    continue
                row_id = str(row.get("id") or row.get("work_item_id") or "")
                identifier = str(row.get("identifier") or row.get("source_id") or "")
                if identifier_or_id in {row_id, identifier}:
                    return {"status": row.get("state", "unknown"), **row}
            for name in ("run_detail", "get_work_detail", "get_run", "get_work_item"):
                method = getattr(self.coordinator, name, None)
                if not callable(method):
                    continue
                try:
                    value = method(identifier_or_id)
                    # The historical route is synchronous; async coordinator
                    # methods are handled by the route's durable snapshot and
                    # do not get represented as a leaked coroutine here.
                    if inspect.isawaitable(value):
                        close = getattr(value, "close", None)
                        if callable(close):
                            close()
                        return None
                    if isinstance(value, dict):
                        return value
                except Exception:
                    logger.exception("AgentWork coordinator run detail failed")
            return None
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

    async def run_detail_async(self, identifier_or_id: str) -> dict[str, Any] | None:
        """Resolve durable work/run detail for async API callers."""

        if self.coordinator is None:
            return self.run_detail(identifier_or_id)
        for name in ("run_detail", "get_work_detail", "get_run", "get_work_item"):
            method = getattr(self.coordinator, name, None)
            if not callable(method):
                continue
            try:
                value = method(identifier_or_id)
                if inspect.isawaitable(value):
                    value = await value
                if isinstance(value, dict):
                    return value
            except Exception:
                logger.exception("AgentWork coordinator async run detail failed")
        return self.run_detail(identifier_or_id)

    async def _collect_finished_runs(self) -> None:
        finished = [
            (item_id, entry)
            for item_id, entry in self.running.items()
            if item_id not in self._stopping and entry.task.done()
        ]
        for item_id, entry in finished:
            # A stop may have completed between the snapshot above and this
            # iteration.  Never process an entry that no longer owns the
            # current running slot.
            if item_id in self._stopping or self.running.get(item_id) is not entry:
                continue
            await self._handle_finished_run(item_id, entry)
            if self.running.get(item_id) is entry:
                self.running.pop(item_id, None)

    async def _handle_finished_run(self, item_id: str, entry: RunningEntry) -> None:
        runtime_seconds = max(0, int((datetime.utcnow() - entry.started_at).total_seconds()))
        self.codex_totals.seconds_running += runtime_seconds
        try:
            result = entry.task.result()
        except asyncio.CancelledError:
            if not self._shutdown_started:
                self._schedule_retry(
                    entry.work_item,
                    self._next_attempt(entry.attempt),
                    "run cancelled",
                )
            return
        except Exception as exc:
            if not self._shutdown_started:
                self._schedule_retry(
                    entry.work_item,
                    self._next_attempt(entry.attempt),
                    str(exc),
                )
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
        elif not self._shutdown_started:
            self._schedule_retry(entry.work_item, self._next_attempt(entry.attempt), result.message)

    async def _reconcile_running(self) -> None:
        await self._reconcile_stalled_runs()
        if self._shutdown_started:
            return
        if not self.running:
            return
        refreshed = await self.tracker.fetch_by_ids(list(self.running.keys()))
        by_id = {item.id: item for item in refreshed}
        for item_id, entry in list(self.running.items()):
            if item_id in self._stopping or self.running.get(item_id) is not entry:
                continue
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

    async def _reconcile_stalled_runs(self) -> None:
        timeout_ms = self.settings.codex.stall_timeout_ms
        if timeout_ms <= 0:
            return
        now = datetime.utcnow()
        for item_id, entry in list(self.running.items()):
            if self._shutdown_started or item_id in self._stopping:
                continue
            last = entry.last_event_at or entry.started_at
            elapsed_ms = int((now - last).total_seconds() * 1000)
            if elapsed_ms > timeout_ms:
                # Await runner completion before publishing retry state.  The
                # runner's finally block owns run_after_run(workspace), so a
                # retry must never overlap that cleanup.
                await self._stop_running(item_id, cleanup_workspace=False)
                if not self._shutdown_started:
                    self._schedule_retry(
                        entry.work_item,
                        self._next_attempt(entry.attempt),
                        f"stalled for {elapsed_ms}ms",
                    )

    async def _stop_running(self, item_id: str, *, cleanup_workspace: bool) -> None:
        # Hold the stop lock through task and workspace cleanup.  A second
        # reconciliation/shutdown request therefore waits for the first one,
        # observes the already-released entry, and cannot cancel/remove twice.
        async with self._stop_lock:
            entry = self.running.get(item_id)
            if entry is None:
                self._release_claim(item_id)
                return
            if item_id in self._stopping:
                # This branch is defensive (the lock normally makes it
                # unreachable) and avoids duplicate work if state is changed
                # by an embedding caller.
                return
            self._stopping.add(item_id)
            caller_cancelled = False
            try:
                try:
                    await self._cancel_and_await(entry.task)
                except asyncio.CancelledError:
                    # The polling/reconcile caller may be cancelled while its
                    # owned runner is being drained.  Keep the cancellation
                    # pending until the workspace cleanup below has finished.
                    caller_cancelled = True
                if cleanup_workspace:
                    try:
                        maybe_cleanup = self.workspace_manager.remove_for(
                            entry.work_item.identifier
                        )
                        if inspect.isawaitable(maybe_cleanup):
                            await maybe_cleanup
                    except Exception:
                        # Workspace cleanup failure must not leave the task or
                        # claim stuck, nor prevent later runs from stopping.
                        logger.exception(
                            "Agent harness workspace cleanup failed for %s",
                            entry.work_item.identifier,
                        )
            finally:
                # Do not expose a running entry after its task/finally and
                # optional workspace cleanup have completed.
                if self.running.get(item_id) is entry:
                    self.running.pop(item_id, None)
                self._stopping.discard(item_id)
                self._release_claim(item_id)
            if caller_cancelled:
                raise asyncio.CancelledError

    async def _cancel_and_await(self, task: asyncio.Task[Any]) -> None:
        """Cancel an owned run task and always retrieve its completion."""

        if not task.done():
            task.cancel()
        caller_cancelled = False
        # Shield the owned task from cancellation of the polling/reconcile
        # caller.  A cancellation can arrive before a freshly-cancelled task
        # has transitioned to ``done``; keep awaiting until the runner's
        # finally block has settled in that case.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # The child itself reports CancelledError only after it is
                # done.  An unfinished child means this waiter was cancelled;
                # defer that cancellation until owner cleanup is complete.
                if not task.done():
                    caller_cancelled = True
                    current = asyncio.current_task()
                    if current is not None:
                        uncancel = getattr(current, "uncancel", None)
                        if callable(uncancel):
                            uncancel()
                continue
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancellation is the expected result of lifecycle stop.
            pass
        except Exception:
            # Retrieve unexpected task failures so asyncio does not report an
            # unhandled ``Task exception was never retrieved`` warning.
            logger.exception("Agent harness run task failed while stopping")
        if caller_cancelled:
            raise asyncio.CancelledError

    async def shutdown(self) -> None:
        """Stop all owned runs and prevent dispatch for this process."""

        if self.coordinator is not None:
            async with self._shutdown_lock:
                if self._shutdown_complete:
                    return
                self._shutdown_started = True
                stop = getattr(self.coordinator, "stop", None) or getattr(
                    self.coordinator, "shutdown", None
                )
                if callable(stop):
                    try:
                        await _maybe_await(stop())
                    except Exception:
                        logger.exception("AgentWork coordinator shutdown failed")
                self._shutdown_complete = True
            return

        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            # Set the flag before awaiting any task so ticks already in flight
            # cannot dispatch another run while shutdown drains the ledger.
            self._shutdown_started = True
            entries = list(self.running.items())
            # Request cancellation for every owned task before awaiting any
            # runner's finally block.  A slow first cleanup must not prevent a
            # later run from receiving its shutdown signal.
            for _item_id, entry in entries:
                if not entry.task.done():
                    entry.task.cancel()
            if entries:
                results = await asyncio.gather(
                    *(
                        self._stop_running(item_id, cleanup_workspace=True)
                        for item_id, _entry in entries
                    ),
                    return_exceptions=True,
                )
                for item_id, result in zip((item_id for item_id, _ in entries), results):
                    if isinstance(result, BaseException):
                        # A malformed embedding or custom stop hook must not
                        # prevent the remaining runs from being drained.
                        logger.error(
                            "Agent harness run shutdown failed for %s: %s",
                            item_id,
                            result,
                            exc_info=(
                                type(result),
                                result,
                                result.__traceback__,
                            ),
                        )
            # Retry state is process-local and must never carry over to a new
            # process.  Durable tracker work items are intentionally untouched.
            self.retry_attempts.clear()
            self.claimed.clear()
            self._shutdown_complete = True

    async def _dispatch_due_retries(self) -> None:
        if self._shutdown_started:
            return
        now = datetime.utcnow()
        due = [
            (item_id, retry)
            for item_id, retry in self.retry_attempts.items()
            if retry.due_at <= now
        ]
        for item_id, retry in sorted(due, key=lambda item: item[1].due_at):
            if self._shutdown_started:
                return
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
        if self._shutdown_started:
            return
        candidates = await self.tracker.fetch_candidates()
        if self._shutdown_started:
            return
        for item in sort_work_items_for_dispatch(candidates):
            if self._shutdown_started:
                return
            if not self._slots_available_for(item):
                break
            if self._should_dispatch(item):
                await self._dispatch(item, attempt=1)

    async def _dispatch(self, item: WorkItem, *, attempt: int | None) -> None:
        if self._shutdown_started:
            return
        attempt_number = attempt or 1
        if self.execution_adapter is not None:
            await self._dispatch_via_execution_adapter(item, attempt=attempt_number)
            return
        enterprise_limits = None
        enterprise_network = None
        if Features.is_enterprise():
            # Enterprise automation has no host-process fallback.  The
            # deployment-owned execution block must explicitly retain the
            # trusted WSL2/bubblewrap backend and the isolated network mode.
            from .config import AGENT_HARNESS_SAFE_EXECUTION_BACKENDS
            from ..security.harness_execution_scope import NetworkCapability, ResourceLimits

            if (
                not self.settings.execution_enabled
                or self.settings.execution_backend
                not in AGENT_HARNESS_SAFE_EXECUTION_BACKENDS
                or self.settings.execution_network not in {"none", "broad"}
            ):
                self._schedule_retry(
                    item,
                    attempt_number,
                    "trusted Enterprise harness execution is disabled or unsupported",
                )
                return
            try:
                enterprise_limits = ResourceLimits.from_values(
                    self.settings.execution_resource_limits
                )
                enterprise_network = NetworkCapability(self.settings.execution_network)
            except Exception as exc:
                self._schedule_retry(
                    item,
                    attempt_number,
                    f"invalid Enterprise harness resource limits: {exc}",
                )
                return
        try:
            workspace, _created = self.workspace_manager.create_for(item.identifier)
            # Harness hooks are deployment-owned control-plane configuration,
            # not model-provided commands.  Keep the established setup flow in
            # both profiles; agent-generated commands still use the sandbox.
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
                if Features.is_enterprise():
                    # Enterprise autonomous runs must never fall through to
                    # the runner's host subprocess lane.  This repository-
                    # specific lower scope is server-issued from the
                    # WorkspaceManager result, never from work-item/model
                    # paths, and forces runner.py through WSL2/bubblewrap.
                    from ..security.agent_run_scope import run_scope_context
                    from ..security.harness_execution_scope import (
                        harness_execution_scope_context,
                    )
                    from ..services.harness_execution_scope_service import (
                        build_autonomous_agent_harness_scope,
                    )

                    run_id = f"agent-harness-{uuid.uuid4().hex}"
                    if enterprise_limits is None:  # pragma: no cover - guarded above
                        raise RuntimeError("Enterprise resource limits were not issued")
                    upper_scope = build_autonomous_agent_harness_scope(
                        workspace_path=workspace,
                        harness_workspace_root=self.settings.workspace_root,
                        resource_limits=enterprise_limits,
                        run_id=run_id,
                        audit_id=f"agent-harness:{item.identifier}:{attempt_number}",
                        network_capability=enterprise_network,
                    )
                    scope = upper_scope.to_agent_run_scope()
                    with harness_execution_scope_context(upper_scope), run_scope_context(
                        scope
                    ):
                        return await self.runner.run(
                            work_item=item,
                            workspace=workspace,
                            prompt=prompt,
                            attempt=attempt_number,
                            on_event=on_event,
                        )
                return await self.runner.run(
                    work_item=item,
                    workspace=workspace,
                    prompt=prompt,
                    attempt=attempt_number,
                    on_event=on_event,
                )
            finally:
                maybe_cleanup = self.workspace_manager.run_after_run(workspace)
                if inspect.isawaitable(maybe_cleanup):
                    await maybe_cleanup

        task = asyncio.create_task(
            run_attempt(),
            name=f"agent-harness-run:{item.identifier}:attempt-{attempt_number}",
        )
        entry = RunningEntry(
            work_item=item,
            workspace_path=workspace,
            task=task,
            attempt=attempt_number,
            started_at=datetime.utcnow(),
        )
        self.running[item.id] = entry
        self.claimed.add(item.id)
        self.retry_attempts.pop(item.id, None)
        # Give the coroutine one scheduling turn before returning from
        # dispatch.  Without this, cancelling a freshly-created task before
        # its first step skips ``run_attempt``'s finally block entirely and
        # leaks the owner-side run_after_run cleanup.
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            # If the polling/reconcile caller itself is cancelled during this
            # hand-off, drain the newly-owned run before propagating the
            # cancellation to that caller.
            if self.running.get(item.id) is entry:
                await self._stop_running(item.id, cleanup_workspace=True)
            raise
        if self._shutdown_started and self.running.get(item.id) is entry:
            await self._stop_running(item.id, cleanup_workspace=True)

    async def _dispatch_via_execution_adapter(
        self,
        item: WorkItem,
        *,
        attempt: int,
    ) -> None:
        """Run one legacy-facade attempt through a one-shot adapter.

        This path is intentionally still process-local because callers only
        reach it when no common coordinator was supplied.  It exists to let
        deployments migrate construction wiring incrementally; durable
        claims/retries remain disabled in the coordinator-backed path.
        """

        adapter = self.execution_adapter
        if adapter is None:  # pragma: no cover - guarded by caller
            return
        try:
            prepare = getattr(adapter, "prepare", None)
            if not callable(prepare):
                raise RuntimeError("execution adapter cannot prepare a workspace")
            prepared = prepare(item, attempt=attempt)
            if inspect.isawaitable(prepared):
                prepared = await prepared
            workspace = Path(getattr(prepared, "workspace"))
        except Exception as exc:
            self._schedule_retry(item, attempt, str(exc))
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

        async def run_attempt() -> Any:
            return await adapter.execute(
                item,
                attempt=attempt,
                on_event=on_event,
                prepared=prepared,
            )

        task = asyncio.create_task(
            run_attempt(),
            name=f"agent-harness-run:{item.identifier}:attempt-{attempt}",
        )
        entry = RunningEntry(
            work_item=item,
            workspace_path=workspace,
            task=task,
            attempt=attempt,
            started_at=datetime.utcnow(),
        )
        self.running[item.id] = entry
        self.claimed.add(item.id)
        self.retry_attempts.pop(item.id, None)
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            if self.running.get(item.id) is entry:
                await self._stop_running(item.id, cleanup_workspace=True)
            raise
        if self._shutdown_started and self.running.get(item.id) is entry:
            await self._stop_running(item.id, cleanup_workspace=True)

    def _should_dispatch(self, item: WorkItem) -> bool:
        return (
            self._is_active(item.state)
            and not self._is_terminal(item.state)
            and not self._shutdown_started
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
        if self._shutdown_started:
            return
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


async def _invoke_common_method(
    method: Any,
    *,
    coordinator: Any,
    work_source: Any | None,
    execution_adapter: Any | None,
    settings: AgentHarnessSettings,
) -> Any:
    """Invoke a coordinator tick across the small WS02 compatibility seam.

    The canonical WS02 coordinator currently exposes ``execute_once(limit=)``
    and keeps source/adapter registration on the coordinator.  Embedders may
    instead expose ``tick(source=..., adapter=...)``.  Inspecting the
    signature lets this facade pass only accepted keyword arguments without
    catching arbitrary TypeErrors raised *inside* a running attempt.
    """

    kwargs: dict[str, Any] = {}
    try:
        signature = inspect.signature(method)
        parameters = signature.parameters
        accepts_var_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
    except (TypeError, ValueError):
        parameters = {}
        accepts_var_kwargs = False

    def add_if_supported(name: str, value: Any) -> None:
        if value is None:
            return
        if accepts_var_kwargs or name in parameters:
            kwargs[name] = value

    add_if_supported("work_source", work_source)
    add_if_supported("source", work_source)
    add_if_supported("execution_adapter", execution_adapter)
    add_if_supported("adapter", execution_adapter)
    add_if_supported("limit", max(1, int(settings.max_concurrent_agents)))
    add_if_supported("max_concurrency", max(1, int(settings.max_concurrent_agents)))

    # If the coordinator owns registration, register the supplied hooks once
    # before executing.  Registration is idempotent in the canonical runtime;
    # errors are logged and the invocation still proceeds for compatibility
    # fakes that intentionally reject optional registration.
    runtime_enabled = bool(getattr(coordinator, "enabled", True))
    if runtime_enabled and work_source is not None:
        register_source = getattr(coordinator, "register_source", None)
        if callable(register_source):
            try:
                source_type = getattr(work_source, "source_type", None)
                value = register_source(work_source, source_type=source_type)
                if inspect.isawaitable(value):
                    await value
            except (TypeError, ValueError):
                try:
                    value = register_source(work_source)
                    if inspect.isawaitable(value):
                        await value
                except Exception:
                    logger.debug("AgentWork source registration skipped", exc_info=True)
            except Exception:
                logger.debug("AgentWork source registration skipped", exc_info=True)
    if runtime_enabled and execution_adapter is not None:
        register_adapter = getattr(coordinator, "register_adapter", None)
        if callable(register_adapter):
            try:
                adapter_key = getattr(execution_adapter, "adapter_key", None)
                value = register_adapter(execution_adapter, adapter_key=adapter_key)
                if inspect.isawaitable(value):
                    await value
            except (TypeError, ValueError):
                try:
                    value = register_adapter(execution_adapter)
                    if inspect.isawaitable(value):
                        await value
                except Exception:
                    logger.debug("AgentWork adapter registration skipped", exc_info=True)
            except Exception:
                logger.debug("AgentWork adapter registration skipped", exc_info=True)

    return await _maybe_await(method(**kwargs))


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
