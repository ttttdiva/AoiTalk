"""Parent-owned Git publication orchestration for coding-agent runs.

The security gate in :mod:`src.security.git_publication_gate` is deliberately
transport agnostic.  This module is the small integration layer used by a
parent controller: it captures the immutable run baseline before workers are
started, exposes read-only preflight decisions, and invokes a supplied parent
transport only after the gate allows it.  Worker-facing code must not receive
this object or a transport callback.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, replace
from os import PathLike
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, TypeVar

from ..security.agent_run_scope import AgentRunScope
from ..security.git_publication_gate import (
    GitPublicationGate,
    PublicationDecision,
    PublicationPreflightDenied,
    RunSnapshot,
    WorkerPublicationDenied,
    assert_worker_publication_denied,
    worker_publication_decision,
)


class ParentPublicationError(RuntimeError):
    """Base error for parent-controller publication orchestration."""


class ParentPublicationNotStarted(ParentPublicationError):
    """Raised when a publication preflight is requested before run start."""


@dataclass(frozen=True, slots=True)
class ParentPublicationState:
    """Immutable, serialisable view of the parent publication baseline."""

    run_id: str
    repository_root: Path
    repository_identity: str
    snapshot: RunSnapshot

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "repository_root": str(self.repository_root),
            "repository_identity": self.repository_identity,
            "snapshot": self.snapshot.as_dict(),
        }

    to_dict = as_dict


TransportResult = TypeVar("TransportResult")
PublicationTransport = Callable[[PublicationDecision], TransportResult]
AsyncPublicationTransport = Callable[
    [PublicationDecision], Awaitable[TransportResult] | TransportResult
]


class ParentGitPublicationController:
    """Parent-only run baseline and Git publication preflight.

    ``GitPublicationGate`` intentionally never runs Git mutation commands.  A
    caller supplies its separately audited parent transport to ``publish`` or
    ``publish_async``.  The callback is invoked only after a successful gate
    decision, and a non-parent actor cannot reach it.
    """

    def __init__(
        self,
        repository_root: str | PathLike[str] | AgentRunScope | None = None,
        *,
        run_scope: AgentRunScope | None = None,
        run_id: str | None = None,
        repository_identity: str | None = None,
        allowed_paths: Iterable[str | PathLike[str]] | None = None,
        parent_actor: str = "parent_controller",
        agent_run_service: Any | None = None,
        parent_run_id: str | None = None,
        trusted_parent_context: Any | None = None,
    ) -> None:
        trusted_scope: AgentRunScope | None = None
        trusted_snapshot: RunSnapshot | None = None
        if trusted_parent_context is not None:
            # The scope factory owns the opaque capability.  Reuse its
            # already-captured snapshot rather than taking a second baseline
            # after workers have started.
            from .agent_run_scope_service import TrustedParentRunContext

            if not isinstance(trusted_parent_context, TrustedParentRunContext):
                raise TypeError("trusted_parent_context must come from the parent scope factory")
            trusted_scope = trusted_parent_context.scope
            trusted_snapshot = trusted_parent_context.snapshot
            requested_parent_id = str(parent_run_id or "").strip()
            if requested_parent_id and requested_parent_id != trusted_parent_context.parent_run_id:
                raise ValueError("trusted_parent_context belongs to a different parent run")
            parent_run_id = trusted_parent_context.parent_run_id
            if repository_root is not None or run_scope is not None:
                raise TypeError(
                    "trusted_parent_context cannot be combined with repository_root/run_scope"
                )
            run_scope = trusted_scope
            repository_root = trusted_scope.canonical_root
        if isinstance(repository_root, AgentRunScope):
            if run_scope is not None and run_scope is not repository_root:
                raise TypeError("repository_root scope and run_scope disagree")
            run_scope = repository_root
            repository_root = run_scope.canonical_root
        if run_scope is not None and not isinstance(run_scope, AgentRunScope):
            raise TypeError("run_scope must be an AgentRunScope")
        requested_run_id = str(run_id or "").strip()
        if run_scope is not None and requested_run_id and run_scope.run_id != requested_run_id:
            raise ValueError("run_scope belongs to a different run_id")
        if repository_root is None and run_scope is None:
            raise ValueError("repository_root or run_scope is required")
        if run_scope is None:
            run_scope = AgentRunScope.for_repository(
                repository_root,
                run_id=run_id,
                repository_identity=repository_identity,
            )
        elif repository_root is not None:
            root = Path(repository_root).resolve()
            if root != run_scope.canonical_root:
                raise ValueError("repository_root must match run_scope canonical root")
        self.run_scope = run_scope
        self.parent_actor = str(parent_actor or "parent_controller").strip()
        if not self.parent_actor:
            raise ValueError("parent_actor is required")
        self.allowed_paths = tuple(allowed_paths) if allowed_paths is not None else None
        self.agent_run_service = agent_run_service
        self.parent_run_id = str(parent_run_id or "").strip() or None
        self._snapshot: RunSnapshot | None = trusted_snapshot
        self._gate: GitPublicationGate | None = (
            GitPublicationGate(
                trusted_snapshot,
                run_scope=self.run_scope,
                allowed_paths=self.allowed_paths,
                parent_actor=self.parent_actor,
            )
            if trusted_snapshot is not None
            else None
        )
        self._snapshot_audit_recorded = False

    @property
    def started(self) -> bool:
        return self._snapshot is not None and self._gate is not None

    @property
    def snapshot(self) -> RunSnapshot | None:
        """Return the immutable baseline, if the parent run has started."""

        return self._snapshot

    @property
    def gate(self) -> GitPublicationGate | None:
        """Return the read-only gate, if the parent run has started."""

        return self._gate

    @property
    def state(self) -> ParentPublicationState | None:
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return ParentPublicationState(
            run_id=snapshot.run_id,
            repository_root=snapshot.canonical_root,
            repository_identity=snapshot.repository_identity,
            snapshot=snapshot,
        )

    def start(self) -> RunSnapshot:
        """Capture the baseline exactly once before worker execution."""

        if self._snapshot is not None:
            return self._snapshot
        snapshot = RunSnapshot.capture(
            self.run_scope,
            repository_identity=self.run_scope.repo_identity,
            run_id=self.run_scope.run_id,
        )
        self._snapshot = snapshot
        self._gate = GitPublicationGate(
            snapshot,
            run_scope=self.run_scope,
            allowed_paths=self.allowed_paths,
            parent_actor=self.parent_actor,
        )
        return snapshot

    async def start_async(self) -> RunSnapshot:
        """Capture the baseline and optionally audit it on the parent AgentRun."""

        snapshot = self.start()
        service = self.agent_run_service
        run_id = self.parent_run_id
        recorder = getattr(service, "record_event", None) if service is not None else None
        if run_id and callable(recorder) and not self._snapshot_audit_recorded:
            result = recorder(
                run_id,
                "run.git_snapshot_captured",
                status="running",
                message="親runのGit baselineを固定しました",
                payload={
                    "run_id": snapshot.run_id,
                    "repository_root": str(snapshot.canonical_root),
                    "repository_identity": snapshot.repository_identity,
                    "baseline": snapshot.baseline_git_state,
                },
            )
            if inspect.isawaitable(result):
                await result
            self._snapshot_audit_recorded = True
        return snapshot

    def _require_gate(self) -> GitPublicationGate:
        gate = self._gate
        if gate is None:
            raise ParentPublicationNotStarted(
                "parent Git publication baseline has not been captured"
            )
        return gate

    @staticmethod
    def _changed_scope_from_report(
        worker_report: Mapping[str, Any] | None,
    ) -> Iterable[str | PathLike[str]] | None:
        if not isinstance(worker_report, Mapping):
            return None
        raw = worker_report.get("changed_scope")
        if raw is None:
            raw = worker_report.get("changed_files")
        if isinstance(raw, (str, PathLike)):
            return (raw,)
        if isinstance(raw, Iterable):
            return tuple(raw)
        return None

    def _actor_for_gate(self, actor: str | None) -> str:
        """Resolve the actor without allowing a worker context to self-escalate."""

        explicit = str(actor or "").strip()
        try:
            # Import lazily to keep this service independent from the normal
            # LLM routing import graph.  Agent Team workers bind this context
            # while they run; a worker cannot override it by passing the
            # parent actor string to a leaked controller object.
            from ..llm.tool_policy import get_current_agent_team_role

            worker_role = str(get_current_agent_team_role() or "").strip()
        except Exception:
            worker_role = ""
        if worker_role:
            return worker_role
        return explicit or self.parent_actor

    def evaluate(
        self,
        *,
        review_approved: bool = False,
        review: Mapping[str, Any] | bool | None = None,
        review_ref: str | None = None,
        worker_actions: Iterable[str] | None = None,
        worker_command: str | None = None,
        worker_commands: Iterable[str] | None = None,
        worker_commit: bool = False,
        worker_push: bool = False,
        force_push: bool = False,
        reset_hard: bool = False,
        clean: bool = False,
        worker_report: Mapping[str, Any] | None = None,
        changed_scope: Iterable[str | PathLike[str]] | None = None,
        actor: str | None = None,
        current_state: Any | None = None,
    ) -> PublicationDecision:
        gate = self._require_gate()
        scope = changed_scope
        if scope is None:
            scope = self._changed_scope_from_report(worker_report)
        decision = gate.evaluate(
            review_approved=review_approved,
            review=review,
            review_ref=review_ref,
            worker_actions=worker_actions,
            worker_command=worker_command,
            worker_commands=worker_commands,
            worker_commit=worker_commit,
            worker_push=worker_push,
            force_push=force_push,
            reset_hard=reset_hard,
            clean=clean,
            worker_report=worker_report,
            changed_scope=scope,
            actor=self._actor_for_gate(actor),
            current_state=current_state,
        )
        background_error = self._background_preflight_error()
        if background_error is None:
            return decision
        return replace(
            decision,
            allowed=False,
            decision="deny",
            reason=background_error,
            reasons=tuple(dict.fromkeys((*decision.reasons, background_error))),
            metadata={
                **dict(decision.metadata),
                "background_jobs": {
                    "allowed": False,
                    "reason": background_error,
                },
            },
        )

    preflight = evaluate
    check = evaluate

    def assert_publishable(self, **kwargs: Any) -> PublicationDecision:
        """Raise the gate denial instead of returning a false decision."""

        kwargs["actor"] = self._actor_for_gate(kwargs.get("actor"))
        if kwargs.get("changed_scope") is None:
            kwargs["changed_scope"] = self._changed_scope_from_report(
                kwargs.get("worker_report")
            )
        decision = self.evaluate(**kwargs)
        if not decision.allowed:
            raise PublicationPreflightDenied(decision.reason)
        return decision

    def publish(
        self,
        transport: PublicationTransport,
        *,
        review_approved: bool = False,
        review: Mapping[str, Any] | bool | None = None,
        review_ref: str | None = None,
        worker_actions: Iterable[str] | None = None,
        worker_report: Mapping[str, Any] | None = None,
        changed_scope: Iterable[str | PathLike[str]] | None = None,
        actor: str | None = None,
        **gate_kwargs: Any,
    ) -> TransportResult:
        """Run the parent gate, then invoke the supplied parent transport.

        The transport receives only the allowlisted ``PublicationDecision``;
        workers cannot bypass the gate by supplying a transport because their
        actor value is rejected before the callback is reached.
        """

        if not callable(transport):
            raise TypeError("parent publication transport must be callable")
        decision = self._assert_parent_publishable(
            review_approved=review_approved,
            review=review,
            review_ref=review_ref,
            worker_actions=worker_actions,
            worker_report=worker_report,
            changed_scope=changed_scope,
            actor=actor,
            **gate_kwargs,
        )
        result = transport(decision)
        if inspect.isawaitable(result):
            raise TypeError("async publication transport requires publish_async")
        return result

    async def publish_async(
        self,
        transport: AsyncPublicationTransport,
        *,
        review_approved: bool = False,
        review: Mapping[str, Any] | bool | None = None,
        review_ref: str | None = None,
        worker_actions: Iterable[str] | None = None,
        worker_report: Mapping[str, Any] | None = None,
        changed_scope: Iterable[str | PathLike[str]] | None = None,
        actor: str | None = None,
        **gate_kwargs: Any,
    ) -> TransportResult:
        """Async counterpart of :meth:`publish` for commit/push adapters."""

        if not callable(transport):
            raise TypeError("parent publication transport must be callable")
        decision = self._assert_parent_publishable(
            review_approved=review_approved,
            review=review,
            review_ref=review_ref,
            worker_actions=worker_actions,
            worker_report=worker_report,
            changed_scope=changed_scope,
            actor=actor,
            **gate_kwargs,
        )
        result = transport(decision)
        if inspect.isawaitable(result):
            return await result
        return result

    def _assert_parent_publishable(self, **kwargs: Any) -> PublicationDecision:
        # Preserve the normal lifecycle error when the parent baseline was
        # never captured; background preflight is meaningful only after this
        # controller owns a publication gate.
        self._require_gate()
        if "actor" in kwargs:
            kwargs["actor"] = self._actor_for_gate(kwargs.get("actor"))
        try:
            return self.assert_publishable(**kwargs)
        except PublicationPreflightDenied:
            raise

    def _background_preflight_error(self) -> str | None:
        """Return a deterministic denial when this run still owns jobs."""

        try:
            from .agent_run_background_jobs import preflight_agent_run_background_jobs

            result = preflight_agent_run_background_jobs(self.run_scope)
        except Exception as exc:
            return f"scoped background-job preflight failed closed: {exc}"
        if bool(result.get("allowed")):
            return None
        return str(
            result.get("reason")
            or f"{result.get('active_count', 0)} scoped background job(s) are still running"
        )

    def worker_publication_decision(self, action: str = "publish") -> Any:
        """Return the unconditional denial exposed to worker boundary tests."""

        return worker_publication_decision(action)

    @staticmethod
    def assert_worker_publication_denied(action: str = "publish") -> None:
        """Keep worker denial available without exposing a transport."""

        assert_worker_publication_denied(action)


# Short compatibility alias for integrations that call this a coordinator.
ParentPublicationController = ParentGitPublicationController


__all__ = [
    "AsyncPublicationTransport",
    "ParentGitPublicationController",
    "ParentPublicationController",
    "ParentPublicationError",
    "ParentPublicationNotStarted",
    "ParentPublicationState",
    "PublicationDecision",
    "PublicationPreflightDenied",
    "PublicationTransport",
    "RunSnapshot",
    "WorkerPublicationDenied",
]
