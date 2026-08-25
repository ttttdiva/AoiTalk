"""Policy-enforcing adapter contract for the QA browser lane.

The browser implementation is injected by the host (Playwright MCP,
computer-use bridge, or another driver).  This adapter owns the security gate:
every navigation/redirect and file transfer is checked before reaching the
driver, and every asynchronous action receives the bounded lease from
``BrowserRunScope``.  It deliberately has no access to the Director profile.
"""

from __future__ import annotations

import inspect
import shutil
import tempfile
import threading
import uuid
from typing import Any, Awaitable, Callable, Mapping

from .browser_scope import BrowserLaneViolation, BrowserRunScope


QA_BROWSER_WORKER_ROLES = frozenset(
    {
        "qa",
        "qa-worker",
        "qa_worker",
        "ui-qa",
        "ui_qa",
        "ui-qa-worker",
        "ui_qa_worker",
        "browser-qa",
        "browser_qa",
        "browser-qa-worker",
        "browser_qa_worker",
    }
)


def _normalise_role(role: str) -> str:
    return str(role or "").strip().casefold().replace(" ", "-")


def _current_agent_team_role() -> str:
    """Read the role binding without making this module require the LLM stack."""

    try:
        from ..llm.tool_policy import get_current_agent_team_role

        return _normalise_role(get_current_agent_team_role() or "")
    except Exception:
        # A policy import failure must not turn a child context into a parent.
        return "agent-team-worker"


def _require_qa_worker_role(role: str) -> str:
    requested = _normalise_role(role)
    requested_key = requested.replace("_", "-")
    allowed_keys = {
        candidate.replace("_", "-") for candidate in QA_BROWSER_WORKER_ROLES
    }
    if requested_key not in allowed_keys:
        raise QABrowserCapabilityError(
            "QA browser capabilities are exposed only to a QA worker role"
        )
    bound = _current_agent_team_role()
    if bound and bound.replace("_", "-") != requested_key:
        raise QABrowserCapabilityError(
            f"Agent Team role {bound!r} cannot receive a QA browser capability"
        )
    return requested


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _call_with_supported_kwargs(
    callback: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Call an injected adapter while tolerating small driver API variants."""

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(*args)
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return callback(*args, **kwargs)
    supported = {
        key: value for key, value in kwargs.items() if key in signature.parameters
    }
    return callback(*args, **supported)


async def _run_trigger(
    trigger: Any,
    *,
    path: str | None = None,
    event_info: Any = None,
) -> Any:
    """Run a locator/callback used to start an upload or download event."""

    if trigger is None:
        raise RuntimeError("a Playwright file event requires a trigger")
    if inspect.isawaitable(trigger):
        return await trigger
    callback = trigger
    if not callable(callback):
        callback = getattr(trigger, "click", None)
    if not callable(callback):
        raise TypeError("browser event trigger must be callable or expose click()")
    try:
        signature = inspect.signature(callback)
        required = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            and parameter.default is inspect.Parameter.empty
        ]
    except (TypeError, ValueError):
        required = []
    if required and event_info is not None:
        first_parameter = required[0].name.casefold()
        if path is not None and any(
            marker in first_parameter for marker in ("path", "file", "name")
        ):
            result = callback(path)
        else:
            result = callback(event_info)
    else:
        result = callback(path) if required and path is not None else callback()
    return await _maybe_await(result)


class QABrowserCapabilityError(BrowserLaneViolation):
    """Raised when a QA browser capability is used outside its worker lane."""


class QABrowserTransport:
    """Small driver-agnostic QA Browser MCP boundary."""

    def __init__(self, scope: BrowserRunScope, driver: Any) -> None:
        if scope.lane_name != "qa":
            raise ValueError("QABrowserTransport requires a QA BrowserRunScope")
        if driver is None:
            raise ValueError("QA browser driver is required")
        if _current_agent_team_role():
            raise QABrowserCapabilityError(
                "Agent Team workers receive a QA capability facade, not a raw browser driver"
            )
        self.scope = scope
        self.driver = driver
        self._closed = False

    async def navigate(self, url: str) -> Any:
        self._ensure_open()
        self.scope.assert_navigation_allowed(url)
        return await self._bounded_call("navigate", self.driver.goto(url))

    async def redirect(self, url: str) -> Any:
        """Redirects are checked exactly like top-level navigation."""

        return await self.navigate(url)

    async def upload(
        self,
        path: str,
        *,
        locator: Any = None,
        file_chooser: Any = None,
        file_chooser_callback: Any = None,
    ) -> Any:
        self._ensure_open()
        safe_path = self.scope.assert_upload_allowed(path)
        callback = getattr(self.driver, "upload", None) or getattr(
            self.driver, "set_input_files", None
        )
        chooser = file_chooser_callback or file_chooser
        if not callable(callback):
            if locator is not None:
                callback = getattr(locator, "set_input_files", None)
            if not callable(callback) and chooser is not None:
                callback = getattr(chooser, "set_files", None) or chooser
        if not callable(callback):
            raise RuntimeError("QA browser driver does not support uploads")
        operation = _call_with_supported_kwargs(
            callback,
            str(safe_path),
            locator=locator,
            file_chooser=chooser,
            file_chooser_callback=chooser,
        )
        return await self._bounded_call("upload", operation)

    async def download(
        self,
        path: str,
        *,
        trigger: Any = None,
        trigger_callback: Any = None,
    ) -> Any:
        self._ensure_open()
        safe_path = self.scope.assert_download_allowed(path)
        callback = getattr(self.driver, "download", None)
        if not callable(callback):
            raise RuntimeError("QA browser driver does not support downloads")
        operation = _call_with_supported_kwargs(
            callback,
            str(safe_path),
            trigger=trigger_callback or trigger,
            trigger_callback=trigger_callback or trigger,
        )
        return await self._bounded_call("download", operation)

    async def action(
        self,
        name: str,
        operation: Awaitable[Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        self._ensure_open()
        if operation is None:
            callback = getattr(self.driver, "action", None)
            if not callable(callback):
                raise RuntimeError("QA driver does not support named actions")
            operation = _call_with_supported_kwargs(callback, name, **kwargs)
        return await self._bounded_call(name, operation)

    async def _bounded_call(self, name: str, operation: Any) -> Any:
        if not inspect.isawaitable(operation):
            async def completed() -> Any:
                return operation

            operation = completed()
        return await self.scope.run_bounded(operation, action=name)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("QA browser transport is closed")
        self.scope._ensure_active()

    async def close(self, reason: str = "QA browser transport closed") -> None:
        if self._closed:
            return
        self._closed = True
        try:
            callback = getattr(self.driver, "close", None)
            if callable(callback):
                result = callback()
                if inspect.isawaitable(result):
                    await self.scope.run_bounded(result, action="close")
        finally:
            self.scope.close(reason)


class QABrowserCapability:
    """Worker-facing facade for one parent-registered QA transport.

    The facade intentionally contains no ``driver``, ``page``, ``context`` or
    ``profile`` attribute.  A parent/controller keeps the raw Playwright
    transport in :class:`QABrowserRegistry`; a QA worker receives only these
    bounded operations.
    """

    __slots__ = ("_registry", "_capability_id", "_role")

    def __init__(self, registry: "QABrowserRegistry", capability_id: str, role: str) -> None:
        self._registry = registry
        self._capability_id = capability_id
        self._role = role

    @property
    def role(self) -> str:
        return self._role

    @property
    def metadata(self) -> Mapping[str, Any]:
        return self._registry._capability_metadata(self._capability_id, self._role)

    async def navigate(self, url: str) -> Any:
        return await self._registry._invoke(
            self._capability_id, self._role, "navigate", url
        )

    async def redirect(self, url: str) -> Any:
        return await self._registry._invoke(
            self._capability_id, self._role, "redirect", url
        )

    async def upload(self, path: str, **kwargs: Any) -> Any:
        return await self._registry._invoke(
            self._capability_id, self._role, "upload", path, **kwargs
        )

    async def download(self, path: str, **kwargs: Any) -> Any:
        return await self._registry._invoke(
            self._capability_id, self._role, "download", path, **kwargs
        )

    async def action(
        self,
        name: str,
        operation: Awaitable[Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        return await self._registry._invoke(
            self._capability_id,
            self._role,
            "action",
            name,
            operation,
            **kwargs,
        )

    async def close(self, reason: str = "QA browser capability closed") -> None:
        await self._registry.revoke(
            self._capability_id, reason=reason, role=self._role
        )


class QABrowserRegistry:
    """Parent-owned registry that exposes only QA worker capabilities.

    ``register`` and ``revoke`` are controller operations.  A worker may use
    an opaque id with ``acquire`` (or receive the result of ``issue``), but the
    returned facade never exposes the underlying driver/profile/context.  A
    bound Agent Team role must itself be one of :data:`QA_BROWSER_WORKER_ROLES`;
    implementers, reviewers, and other workers cannot request a QA browser.
    """

    def __init__(self, *, principal: str = "parent") -> None:
        requested = _normalise_role(principal)
        if requested not in {"parent", "controller"}:
            raise QABrowserCapabilityError(
                "QA browser registry is parent/controller owned"
            )
        if _current_agent_team_role():
            raise QABrowserCapabilityError(
                "Agent Team workers cannot create a QA browser registry"
            )
        self._principal = requested
        self._entries: dict[str, QABrowserTransport] = {}
        self._lock = threading.RLock()

    def register(
        self,
        transport: QABrowserTransport,
        *,
        capability_id: str | None = None,
    ) -> str:
        """Register one QA transport and return an opaque worker handle."""

        self._ensure_parent()
        if not isinstance(transport, QABrowserTransport):
            raise TypeError("QA browser registry accepts QABrowserTransport only")
        if transport.scope.lane_name != "qa":
            raise QABrowserCapabilityError(
                "Director browser transports cannot be registered in the QA registry"
            )
        transport.scope._ensure_active()
        identifier = str(capability_id or f"qa-cap-{uuid.uuid4().hex}").strip()
        if (
            not identifier
            or len(identifier) > 240
            or any(character in identifier for character in "\\/\x00\r\n")
        ):
            raise ValueError("invalid QA browser capability id")
        with self._lock:
            if identifier in self._entries:
                raise QABrowserCapabilityError(
                    f"QA browser capability already registered: {identifier}"
                )
            self._entries[identifier] = transport
        return identifier

    def acquire(self, capability_id: str, *, role: str = "qa_worker") -> QABrowserCapability:
        """Return a role-bound facade without exposing the raw transport."""

        worker_role = _require_qa_worker_role(role)
        identifier = str(capability_id or "").strip()
        with self._lock:
            if identifier not in self._entries:
                raise QABrowserCapabilityError("unknown or revoked QA browser capability")
        return QABrowserCapability(self, identifier, worker_role)

    def issue(
        self,
        transport: QABrowserTransport,
        *,
        role: str = "qa_worker",
        capability_id: str | None = None,
    ) -> QABrowserCapability:
        """Register and immediately issue a worker-facing capability."""

        self._ensure_parent()
        _require_qa_worker_role(role)
        identifier = self.register(transport, capability_id=capability_id)
        try:
            return self.acquire(identifier, role=role)
        except Exception:
            with self._lock:
                self._entries.pop(identifier, None)
            raise

    async def revoke(
        self,
        capability_id: str,
        *,
        reason: str = "QA browser capability revoked",
        role: str | None = None,
    ) -> None:
        """Close and remove a capability; QA workers may close their own one."""

        if role is not None:
            _require_qa_worker_role(role)
        else:
            self._ensure_parent()
        identifier = str(capability_id or "").strip()
        with self._lock:
            transport = self._entries.pop(identifier, None)
        if transport is not None:
            await transport.close(reason)

    async def close_all(self, reason: str = "QA browser registry closed") -> None:
        self._ensure_parent()
        with self._lock:
            transports = list(self._entries.values())
            self._entries.clear()
        first_error: Exception | None = None
        for transport in transports:
            try:
                await transport.close(reason)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _ensure_parent(self) -> None:
        if _current_agent_team_role():
            raise QABrowserCapabilityError(
                "Agent Team workers cannot mutate the QA browser registry"
            )

    def _transport_for(self, capability_id: str, role: str) -> QABrowserTransport:
        _require_qa_worker_role(role)
        with self._lock:
            transport = self._entries.get(str(capability_id or "").strip())
        if transport is None:
            raise QABrowserCapabilityError("unknown or revoked QA browser capability")
        return transport

    async def _invoke(
        self, capability_id: str, role: str, method: str, *args: Any, **kwargs: Any
    ) -> Any:
        transport = self._transport_for(capability_id, role)
        callback = getattr(transport, method, None)
        if not callable(callback):
            raise QABrowserCapabilityError(f"unsupported QA browser operation: {method}")
        return await callback(*args, **kwargs)

    def _capability_metadata(self, capability_id: str, role: str) -> Mapping[str, Any]:
        transport = self._transport_for(capability_id, role)
        metadata = transport.scope.lifecycle_metadata()
        # Keep the worker-facing projection free of profile/process identities
        # and filesystem paths.  The parent can inspect the transport itself.
        return {
            "lane": metadata["lane"],
            "run_id": metadata["run_id"],
            "principal": metadata["principal"],
            "state": metadata["state"],
            "allowed_origins": metadata["allowed_origins"],
        }


# Name used by coordinators that model the registry as a capability broker.
QABrowserCapabilityRegistry = QABrowserRegistry


def create_qa_browser_registry(**kwargs: Any) -> QABrowserRegistry:
    """Create a parent-owned QA registry without exposing a Director lane."""

    return QABrowserRegistry(**kwargs)


class _PlaywrightQADriver:
    """Small Playwright driver owned by one QA transport/profile."""

    def __init__(
        self,
        context: Any,
        page: Any,
        profile_dir: str,
        *,
        upload_locator: Any = None,
        upload_file_chooser: Any = None,
        download_trigger: Any = None,
    ) -> None:
        self.context = context
        self.page = page
        self.profile_dir = profile_dir
        self.upload_locator = upload_locator
        self.upload_file_chooser = upload_file_chooser
        self.download_trigger = download_trigger

    async def goto(self, url: str) -> Any:
        return await self.page.goto(url, wait_until="domcontentloaded")

    def _action_locator(self, selector: Any) -> Any:
        if selector is None:
            return self.page
        if not isinstance(selector, str):
            return selector
        locator_factory = getattr(self.page, "locator", None)
        if not callable(locator_factory):
            raise RuntimeError("Playwright QA page does not support locators")
        return locator_factory(selector)

    async def action(
        self,
        name: str,
        *,
        selector: Any = None,
        value: Any = None,
        timeout_ms: int | None = None,
    ) -> Any:
        """Perform one bounded, named Playwright action for the QA facade.

        The parent owns the raw page.  Workers receive only this small action
        vocabulary through :class:`QABrowserCapability`; arbitrary Python
        callbacks never cross that boundary.
        """

        action_name = str(name or "").strip().casefold()
        if action_name in {"wait", "sleep"}:
            import asyncio

            await asyncio.sleep(max(float(value or 0), 0.0))
            return {"waited_seconds": max(float(value or 0), 0.0)}
        if action_name in {"snapshot", "content"}:
            content = getattr(self.page, "content", None)
            if not callable(content):
                raise RuntimeError("Playwright QA page does not support content()")
            return await _maybe_await(content())
        locator = self._action_locator(selector)
        if action_name == "click":
            return await _maybe_await(_call_with_supported_kwargs(
                locator.click,
                timeout_ms=timeout_ms,
            ))
        if action_name in {"fill", "type"}:
            method = getattr(locator, action_name, None)
            if not callable(method):
                raise RuntimeError(f"Playwright locator does not support {action_name}()")
            return await _maybe_await(_call_with_supported_kwargs(
                method,
                str(value or ""),
                timeout_ms=timeout_ms,
            ))
        if action_name == "press":
            method = getattr(locator, "press", None)
            if not callable(method):
                raise RuntimeError("Playwright locator does not support press()")
            return await _maybe_await(_call_with_supported_kwargs(
                method,
                str(value or ""),
                timeout_ms=timeout_ms,
            ))
        raise ValueError(f"unsupported QA action: {name}")

    def _resolve_locator(self, locator: Any) -> Any:
        selected = self.upload_locator if locator is None else locator
        if isinstance(selected, str):
            locator_factory = getattr(self.page, "locator", None)
            if not callable(locator_factory):
                raise RuntimeError("Playwright QA page does not support locators")
            return locator_factory(selected)
        return selected

    def _resolve_trigger(self, trigger: Any) -> Any:
        if isinstance(trigger, str):
            locator_factory = getattr(self.page, "locator", None)
            if not callable(locator_factory):
                raise RuntimeError("Playwright QA page does not support locators")
            return locator_factory(trigger)
        return trigger

    async def upload(
        self,
        path: str,
        *,
        locator: Any = None,
        file_chooser: Any = None,
        file_chooser_callback: Any = None,
    ) -> Any:
        selected_locator = self._resolve_locator(locator)
        if selected_locator is not None:
            setter = getattr(selected_locator, "set_input_files", None)
            if not callable(setter):
                raise RuntimeError("Playwright upload locator lacks set_input_files()")
            return await _maybe_await(setter(path))

        chooser_trigger = (
            file_chooser_callback
            or file_chooser
            or self.upload_file_chooser
        )
        chooser_trigger = self._resolve_trigger(chooser_trigger)
        if chooser_trigger is None:
            raise RuntimeError(
                "Playwright QA driver requires an upload locator or file chooser callback"
            )
        # A caller may pass an already-created FileChooser object.  Prefer its
        # direct set_files() method over installing a second event waiter.
        setter = getattr(chooser_trigger, "set_files", None)
        if callable(setter):
            return await _maybe_await(setter(path))

        expect_factory = getattr(self.page, "expect_file_chooser", None)
        if not callable(expect_factory):
            expect_factory = getattr(self.context, "expect_file_chooser", None)
        if not callable(expect_factory):
            raise RuntimeError(
                "Playwright QA page/context does not support file chooser events"
            )
        chooser_event = await _maybe_await(expect_factory())
        async with chooser_event as chooser_info:
            trigger_result = await _run_trigger(
                chooser_trigger, path=path, event_info=chooser_info
            )
        chooser = trigger_result
        if not callable(getattr(chooser, "set_files", None)):
            chooser = await _maybe_await(getattr(chooser_info, "value", chooser_info))
        setter = getattr(chooser, "set_files", None)
        if not callable(setter):
            raise RuntimeError("Playwright file chooser lacks set_files()")
        return await _maybe_await(setter(path))

    async def download(
        self,
        path: str,
        *,
        trigger: Any = None,
        trigger_callback: Any = None,
    ) -> Any:
        download_trigger = trigger_callback or trigger or self.download_trigger
        download_trigger = self._resolve_trigger(download_trigger)
        if download_trigger is None:
            raise RuntimeError(
                "Playwright QA driver requires a download trigger callback or locator"
            )
        expect_factory = getattr(self.context, "expect_download", None)
        if not callable(expect_factory):
            expect_factory = getattr(self.page, "expect_download", None)
        if not callable(expect_factory):
            raise RuntimeError(
                "Playwright QA page/context does not support download events"
            )
        download_event = await _maybe_await(expect_factory())
        async with download_event as download_info:
            trigger_result = await _run_trigger(
                download_trigger, path=path, event_info=download_info
            )
        download = trigger_result
        if not callable(getattr(download, "save_as", None)):
            download = await _maybe_await(getattr(download_info, "value", download_info))
        save_as = getattr(download, "save_as", None)
        if not callable(save_as):
            raise RuntimeError("Playwright download object lacks save_as()")
        await _maybe_await(save_as(path))
        return path

    async def close(self) -> None:
        try:
            await _maybe_await(self.context.close())
        finally:
            shutil.rmtree(self.profile_dir, ignore_errors=True)


async def launch_playwright_qa_transport(
    scope: BrowserRunScope,
    playwright: Any,
    *,
    headless: bool = True,
    upload_locator: Any = None,
    upload_file_chooser: Any = None,
    download_trigger: Any = None,
) -> QABrowserTransport:
    """Launch a fresh, profile-isolated Playwright QA browser.

    The caller supplies a Playwright implementation (Python Playwright or an
    MCP bridge).  The returned transport owns a temporary profile and installs
    a request/redirect origin guard before exposing the page to the caller.
    """

    if scope.lane_name != "qa":
        raise ValueError("Playwright QA transport requires a QA scope")
    scope._ensure_active()
    profile_dir = tempfile.mkdtemp(prefix=f"aoi-qa-{scope.run_id}-")
    context: Any = None
    try:
        from ..utils.subprocess_env import build_aoitalk_subprocess_env

        context = await _maybe_await(
            playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=headless,
                env=build_aoitalk_subprocess_env(),
            )
        )

        async def guard(route: Any, request: Any) -> None:
            try:
                scope.assert_navigation_allowed(str(request.url))
            except Exception:
                await _maybe_await(route.abort())
                return
            await _maybe_await(route.continue_())

        await _maybe_await(context.route("**/*", guard))
        page = (
            context.pages[0]
            if context.pages
            else await _maybe_await(context.new_page())
        )
        return QABrowserTransport(
            scope,
            _PlaywrightQADriver(
                context,
                page,
                profile_dir,
                upload_locator=upload_locator,
                upload_file_chooser=upload_file_chooser,
                download_trigger=download_trigger,
            ),
        )
    except Exception:
        if context is not None:
            try:
                await _maybe_await(context.close())
            except Exception:
                pass
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise


__all__ = [
    "QA_BROWSER_WORKER_ROLES",
    "QABrowserCapability",
    "QABrowserCapabilityError",
    "QABrowserCapabilityRegistry",
    "QABrowserRegistry",
    "QABrowserTransport",
    "create_qa_browser_registry",
    "launch_playwright_qa_transport",
]
