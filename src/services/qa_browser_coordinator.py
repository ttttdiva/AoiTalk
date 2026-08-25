"""Parent-owned lifecycle wiring for the Agent Team QA browser lane.

The security primitives in :mod:`src.security.browser_scope` and
:mod:`src.security.qa_browser_transport` intentionally do not know how an
Agent Team run is assembled.  This module is the small parent/controller
facade that joins those primitives together:

``QABrowserScope`` -> transport launcher -> ``QABrowserRegistry`` -> opaque
``QABrowserCapability``.

Only the capability facade is ever copied into a child runtime context.  The
transport, Playwright page/context, and temporary profile remain private to
this parent-owned coordinator until :meth:`close` revokes the capability.
"""

from __future__ import annotations

import inspect
import os
from uuid import uuid4
from typing import Any, Callable, Mapping, Sequence

from ..security.agent_run_scope import AgentRunScope
from ..security.browser_scope import BrowserOrigin, BrowserRunScope, QABrowserScope
from ..security.qa_browser_transport import (
    QA_BROWSER_WORKER_ROLES,
    QABrowserCapability,
    QABrowserCapabilityError,
    QABrowserRegistry,
    QABrowserTransport,
    launch_playwright_qa_transport,
)
from .agent_run_scope_service import TrustedParentRunContext


def _normalise_role(role: str) -> str:
    return str(role or "").strip().casefold().replace(" ", "-")


def _qa_role(role: str) -> str:
    """Validate a worker role before issuing a capability to it."""

    requested = str(role or "").strip()
    requested_key = _normalise_role(requested).replace("_", "-")
    allowed = {
        _normalise_role(value).replace("_", "-") for value in QA_BROWSER_WORKER_ROLES
    }
    if requested_key not in allowed:
        raise QABrowserCapabilityError(
            "QA browser capabilities are exposed only to a QA worker role"
        )
    # Keep the canonical production role in contexts unless a caller
    # deliberately selected another role from the security allow-list.
    return requested or "ui_qa_worker"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _launch_transport(
    launcher: Callable[..., Any],
    scope: BrowserRunScope,
    *,
    playwright: Any = None,
    launch_kwargs: Mapping[str, Any] | None = None,
) -> QABrowserTransport:
    """Invoke the default or injected launcher without leaking raw objects.

    Production uses :func:`launch_playwright_qa_transport`, which takes a
    scope and a Playwright bridge.  Tests can inject a compact ``factory``
    accepting just ``scope`` (or ``scope, playwright``) and return a
    :class:`QABrowserTransport` backed by a fake driver.
    """

    kwargs = dict(launch_kwargs or {})
    if playwright is not None:
        kwargs.setdefault("playwright", playwright)
    try:
        signature = inspect.signature(launcher)
    except (TypeError, ValueError):
        signature = None

    if signature is not None:
        parameters = list(signature.parameters.values())
        has_var_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        has_var_args = any(
            parameter.kind is inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters
        )
        if has_var_kwargs:
            result = launcher(scope, **kwargs)
        elif has_var_args:
            result = launcher(scope, playwright, **kwargs)
        else:
            # Do not pass an unused Playwright argument to a test factory that
            # intentionally only accepts ``scope``.  Named kwargs are filtered
            # in the same way so launcher-specific options stay injectable.
            accepted = {
                name
                for name, parameter in signature.parameters.items()
                if name != "scope"
                and parameter.kind
                in {
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                }
            }
            filtered = {name: value for name, value in kwargs.items() if name in accepted}
            positional = [scope]
            if "playwright" in accepted and "playwright" not in filtered:
                # ``playwright=None`` is meaningful to an explicitly
                # declared parameter and keeps the call deterministic.
                filtered["playwright"] = playwright
            result = launcher(*positional, **filtered)
    else:
        # C-extension/callable objects may not expose a signature.  Prefer the
        # production shape and let a genuine invocation error propagate.
        result = launcher(scope, **kwargs)

    transport = await _maybe_await(result)
    if not isinstance(transport, QABrowserTransport):
        raise TypeError("QA browser launcher must return QABrowserTransport")
    return transport


class QABrowserCoordinator:
    """Parent-owned QA browser capability lease for one Agent Team run.

    The coordinator is intentionally the only object that keeps the raw
    transport.  ``inject_into_project_context`` copies an opaque capability
    facade and no transport/profile/page references into the child context.
    ``close`` is idempotent and revokes the registry entry before closing the
    underlying transport/profile.
    """

    __slots__ = (
        "scope",
        "registry",
        "capability",
        "capability_id",
        "role",
        "_transport",
        "_trusted_parent_context",
        "_closed",
    )

    def __init__(
        self,
        *,
        scope: QABrowserScope,
        registry: QABrowserRegistry,
        capability: QABrowserCapability,
        capability_id: str,
        role: str,
        transport: QABrowserTransport,
        trusted_parent_context: TrustedParentRunContext | None = None,
    ) -> None:
        self.scope = scope
        self.registry = registry
        self.capability = capability
        self.capability_id = str(capability_id)
        self.role = str(role)
        self._transport = transport
        self._trusted_parent_context = trusted_parent_context
        self._closed = False

    @classmethod
    async def create(
        cls,
        *,
        allowed_origins: Sequence[str | BrowserOrigin] | str | BrowserOrigin | None = None,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        scope: QABrowserScope | None = None,
        browser_scope: QABrowserScope | None = None,
        agent_run_scope: AgentRunScope | None = None,
        repository_scope: AgentRunScope | None = None,
        repository_root: str | os.PathLike[str] | None = None,
        trusted_parent_context: TrustedParentRunContext | None = None,
        principal: str = "parent",
        role: str = "ui_qa_worker",
        capability_id: str | None = None,
        playwright: Any = None,
        driver: Any = None,
        transport: QABrowserTransport | None = None,
        launcher: Callable[..., Any] | None = None,
        transport_factory: Callable[..., Any] | None = None,
        launch_kwargs: Mapping[str, Any] | None = None,
        action_timeout_seconds: float = 15.0,
        max_action_timeout_seconds: float = 60.0,
        max_lifetime_seconds: float | None = 30 * 60.0,
    ) -> "QABrowserCoordinator":
        """Create and issue one QA capability from a parent controller.

        ``trusted_parent_context`` is preferred for production because it
        binds the browser path gates to the immutable parent AgentRun scope.
        A directly supplied :class:`AgentRunScope` is also accepted for
        parent-owned integrations and tests.  No model/project path is ever
        promoted to a scope here.
        """

        if trusted_parent_context is not None and not isinstance(
            trusted_parent_context, TrustedParentRunContext
        ):
            raise TypeError("trusted_parent_context must be a TrustedParentRunContext")
        if run_id and parent_run_id and str(run_id).strip() != str(parent_run_id).strip():
            raise ValueError("run_id and parent_run_id must match")
        provided_browser_scope = scope or browser_scope
        if provided_browser_scope is not None and not isinstance(
            provided_browser_scope, BrowserRunScope
        ):
            raise TypeError("scope must be a QABrowserScope")
        if provided_browser_scope is not None and provided_browser_scope.lane_name != "qa":
            raise QABrowserCapabilityError("QA coordinator requires a QA browser scope")
        if transport is not None and not isinstance(transport, QABrowserTransport):
            raise TypeError("transport must be a QABrowserTransport")
        if transport is not None and transport.scope.lane_name != "qa":
            raise QABrowserCapabilityError("QA coordinator requires a QA transport")
        if transport is not None and (launcher is not None or transport_factory is not None or driver is not None):
            raise ValueError("pass one of transport, launcher, transport_factory, or driver")
        if transport is not None:
            if provided_browser_scope is not None and transport.scope is not provided_browser_scope:
                raise ValueError("transport scope and supplied QABrowserScope must match")
            provided_browser_scope = provided_browser_scope or transport.scope
        bound_run_scope = trusted_parent_context.scope if trusted_parent_context else None
        direct_scope = agent_run_scope or repository_scope
        if provided_browser_scope is not None and (
            bound_run_scope is not None or direct_scope is not None or repository_root is not None
        ):
            raise ValueError(
                "pass one of scope, repository_root, agent_run_scope, or trusted_parent_context"
            )
        if repository_root is not None and (bound_run_scope is not None or direct_scope is not None):
            raise ValueError(
                "pass one of repository_root, agent_run_scope, or trusted_parent_context"
            )
        if bound_run_scope is not None and direct_scope is not None:
            if bound_run_scope is not direct_scope:
                raise ValueError(
                    "pass either trusted_parent_context or agent_run_scope, not both"
                )
        bound_run_scope = bound_run_scope or direct_scope

        clean_run_id = str(run_id or parent_run_id or "").strip() or None
        if repository_root is not None:
            if not clean_run_id:
                raise ValueError("repository_root requires run_id or parent_run_id")
            bound_run_scope = AgentRunScope.for_repository(
                repository_root,
                run_id=clean_run_id,
            )
        if trusted_parent_context is not None:
            expected = trusted_parent_context.parent_run_id
            if clean_run_id and clean_run_id != expected:
                raise ValueError(
                    "QA browser run_id must match the trusted parent AgentRun"
                )
            clean_run_id = expected
        elif bound_run_scope is not None:
            if clean_run_id and clean_run_id != bound_run_scope.run_id:
                raise ValueError(
                    "QA browser run_id must match the bound AgentRunScope"
                )
            clean_run_id = bound_run_scope.run_id

        if provided_browser_scope is not None:
            if clean_run_id and clean_run_id != provided_browser_scope.run_id:
                raise ValueError(
                    "QA browser run_id must match the supplied QABrowserScope"
                )
            clean_run_id = provided_browser_scope.run_id

        if provided_browser_scope is None:
            if allowed_origins is None:
                raise ValueError("allowed_origins is required when scope is not supplied")
            browser_scope = QABrowserScope.for_qa(
                run_id=clean_run_id,
                allowed_origins=allowed_origins,
                agent_run_scope=bound_run_scope,
                principal=principal,
                action_timeout_seconds=action_timeout_seconds,
                max_action_timeout_seconds=max_action_timeout_seconds,
                max_lifetime_seconds=max_lifetime_seconds,
            )
        else:
            browser_scope = provided_browser_scope
        scope = browser_scope
        selected_launcher = transport_factory or launcher
        if selected_launcher is None and driver is not None:
            # Parent-only test/integration seam.  The raw driver is wrapped
            # immediately and is never copied into a worker context.
            def _driver_launcher(scope: BrowserRunScope) -> QABrowserTransport:
                return QABrowserTransport(scope, driver)

            selected_launcher = _driver_launcher

        if selected_launcher is None:
            selected_launcher = launch_playwright_qa_transport

        registry = QABrowserRegistry(principal="parent")
        active_transport: QABrowserTransport | None = transport
        try:
            if active_transport is None:
                active_transport = await _launch_transport(
                    selected_launcher,
                    scope,
                    playwright=playwright,
                    launch_kwargs=launch_kwargs,
                )
            if active_transport.scope is not scope:
                raise QABrowserCapabilityError(
                    "QA browser launcher returned a transport for a different scope"
                )
            identifier = str(capability_id or f"qa-runtime-{uuid4().hex}")
            capability = registry.issue(
                active_transport,
                role=_qa_role(role),
                capability_id=identifier,
            )
            return cls(
                scope=scope,
                registry=registry,
                capability=capability,
                capability_id=identifier,
                role=_qa_role(role),
                transport=active_transport,
                trusted_parent_context=trusted_parent_context,
            )
        except Exception:
            if active_transport is not None:
                try:
                    await active_transport.close("QA browser coordinator setup failed")
                except Exception:
                    pass
            else:
                scope.close("QA browser coordinator setup failed")
            raise

    # ``open``/``start`` are equivalent parent-factory spellings used by
    # integrations that model the lease as an async resource.
    open = create
    start = create

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def transport(self) -> QABrowserTransport:
        """Return the parent-owned transport for controller diagnostics only."""

        if self._closed:
            raise RuntimeError("QA browser coordinator is closed")
        return self._transport

    def inject_into_project_context(
        self,
        project_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return trusted runtime context carrying only the worker facade."""

        if self._closed:
            raise RuntimeError("QA browser coordinator is closed")
        context = dict(project_context or {})
        for key in ("qa_browser_capability", "_qa_browser_capability"):
            existing = context.get(key)
            if existing is not None and existing is not self.capability:
                raise QABrowserCapabilityError(
                    "project context contains a different QA browser capability"
                )
        # Keep the public key for the runtime bridge and a private alias for
        # callers that explicitly strip model-facing fields.  Both values are
        # the same opaque facade; no raw transport/page/profile is copied.
        context["qa_browser_capability"] = self.capability
        context["_qa_browser_capability"] = self.capability
        return context

    bind_project_context = inject_into_project_context
    to_project_context = inject_into_project_context
    project_context_with_capability = inject_into_project_context

    def build_runtime_tool_registry(
        self,
        config: Any,
        project_context: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Build the production runtime registry with this QA lease bound."""

        from ..llm.runtime_tool_registry import build_runtime_tool_registry

        context = self.inject_into_project_context(project_context)
        kwargs.setdefault("qa_browser_coordinator", self)
        if self._trusted_parent_context is not None:
            kwargs.setdefault("trusted_parent_context", self._trusted_parent_context)
        return build_runtime_tool_registry(config, project_context=context, **kwargs)

    async def close(self, reason: str = "QA browser coordinator closed") -> None:
        """Revoke the worker capability and close its transport/profile."""

        if self._closed:
            return
        self._closed = True
        revoke_error: BaseException | None = None
        try:
            # This is a parent-owned cleanup path.  Omitting ``role`` keeps
            # revocation on the parent/controller side even when a caller is
            # unwinding a child task's context.
            await self.registry.revoke(self.capability_id, reason=reason)
        except BaseException as exc:
            revoke_error = exc
            # A custom/partially failed registry must not leave a browser
            # profile alive merely because its bookkeeping failed.
            try:
                await self._transport.close(reason)
            except Exception:
                pass
        finally:
            # ``revoke`` owns transport.close().  If a custom registry was
            # already closed or replaced, make the parent cleanup idempotent.
            if not self.scope.closed:
                self.scope.close(reason)
        if revoke_error is not None:
            raise revoke_error

    async def __aenter__(self) -> "QABrowserCoordinator":
        return self

    async def __aexit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        await self.close("QA browser coordinator context exited")


async def create_qa_browser_coordinator(**kwargs: Any) -> QABrowserCoordinator:
    """Async parent factory used by runtime integrations and tests."""

    return await QABrowserCoordinator.create(**kwargs)


create_qa_browser_runtime = create_qa_browser_coordinator
start_qa_browser_coordinator = create_qa_browser_coordinator
open_qa_browser_coordinator = create_qa_browser_coordinator


__all__ = [
    "QABrowserCoordinator",
    "create_qa_browser_coordinator",
    "create_qa_browser_runtime",
    "open_qa_browser_coordinator",
    "start_qa_browser_coordinator",
]
