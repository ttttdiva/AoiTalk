"""Deterministic security gates for the two browser lanes.

The application has two deliberately different browser responsibilities:

``director``
    The existing Playwright browser used to talk to ChatGPT Web.  It belongs
    to the parent/controller only and may use a ChatGPT origin.

``qa``
    A short-lived browser used by a local UI-QA worker.  It receives an exact
    allow-list of the AoiTalk origins for the current run.  A QA scope never
    permits ChatGPT Web, ``file://``, or an unconfigured external service.

This module is a policy/gate API, not a browser transport.  A Playwright or
MCP adapter should call :meth:`BrowserRunScope.assert_navigation_allowed`
before every navigation (including redirects), and the upload/download gates
before touching a path.  The checks are intentionally independent of a
browser implementation so they can be tested without launching a browser.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .agent_run_scope import (
    AgentRunScope,
    RunScopeViolation,
    get_current_run_scope,
)


class BrowserScopeError(ValueError):
    """Base class for malformed browser-scope configuration or requests."""


class BrowserLaneViolation(PermissionError, BrowserScopeError):
    """Raised when a caller tries to use the wrong browser lane."""


class BrowserOriginViolation(PermissionError, BrowserScopeError):
    """Raised when a URL is not allowed by the run's origin policy."""


class BrowserPathViolation(PermissionError, BrowserScopeError):
    """Raised when a browser file operation is not in ``AgentRunScope``."""


class BrowserLifecycleViolation(RuntimeError, BrowserScopeError):
    """Raised after a browser run has expired, closed, or exceeded a budget."""


class BrowserLane(str, Enum):
    """The two security-separated browser lanes."""

    DIRECTOR = "director"
    QA = "qa"


_DEFAULT_DIRECTOR_ORIGIN = "https://chatgpt.com"
_DEFAULT_ACTION_TIMEOUT_SECONDS = 15.0
_DEFAULT_MAX_ACTION_TIMEOUT_SECONDS = 60.0
_DEFAULT_MAX_LIFETIME_SECONDS = 30 * 60.0
_DEFAULT_PORTS = {"http": 80, "https": 443}

# Director creation is a parent-only capability.  The opaque identity is
# deliberately not a string or a role name, so a worker cannot acquire the
# lane merely by claiming to be ``parent`` in a request payload.  The
# controller imports this private symbol at its trusted boundary; adapters
# and tests that do not hold it can only create a QA scope.
_DIRECTOR_SCOPE_CAPABILITY = object()

# A worker is intentionally a deny-list rather than a role-name convention:
# Agent Team role identifiers are extensible, and any unrecognised role must
# not become a way to acquire the Director lane.
_WORKER_PRINCIPALS = frozenset(
    {
        "worker",
        "local_worker",
        "local-worker",
        "qa",
        "qa_worker",
        "qa-worker",
        "agent_team",
        "agent-team",
        "subagent",
        "sub_agent",
        "sub-agent",
    }
)
_DIRECTOR_PRINCIPALS = frozenset(
    {
        "parent",
        "controller",
        "director",
    }
)


def _normalise_principal(value: str | None) -> str:
    principal = str(value or "").strip().casefold()
    if not principal:
        raise BrowserLaneViolation("browser scope requires an explicit principal")
    return principal


def _agent_team_role_bound() -> bool:
    """Return whether this call is running inside a local Agent Team child.

    The import is intentionally lazy: ``tool_policy`` imports a broad set of
    runtime modules, while this small security gate must remain importable by
    the browser adapters themselves.  A missing policy module means there is
    no role binding; an explicitly bound role is always treated as a worker
    context and therefore cannot acquire the Director capability.
    """

    try:
        from src.llm.tool_policy import get_current_agent_team_role
    except ImportError:
        return False
    except Exception:
        # A policy import failure must not become a Director capability leak.
        return True
    return bool(str(get_current_agent_team_role() or "").strip())


def _normalise_lane(value: BrowserLane | str) -> BrowserLane:
    if isinstance(value, BrowserLane):
        return value
    try:
        return BrowserLane(str(value).strip().casefold())
    except ValueError as exc:
        raise BrowserScopeError(f"unsupported browser lane: {value!r}") from exc


def _normalise_host(host: str) -> str:
    value = str(host or "").strip().rstrip(".").casefold()
    if not value:
        raise BrowserScopeError("browser origin must include a host")
    # urlsplit.hostname strips brackets from IPv6.  Zone identifiers make an
    # origin machine-local and are not stable across browser contexts, so do
    # not admit them into an allow-list.
    if "%" in value:
        raise BrowserScopeError(
            "browser origin host may not contain an IPv6 zone identifier"
        )
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            value = value.encode("idna").decode("ascii").casefold()
        except (UnicodeError, ValueError) as exc:
            raise BrowserScopeError(f"invalid browser origin host: {host!r}") from exc
        if any(character.isspace() for character in value) or "*" in value:
            raise BrowserScopeError(f"invalid browser origin host: {host!r}")
        return value
    return address.compressed.casefold()


def _format_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _is_chatgpt_host(host: str) -> bool:
    return host == "chatgpt.com" or host.endswith(".chatgpt.com")


@dataclass(frozen=True, order=True, slots=True)
class BrowserOrigin:
    """Canonical scheme/host/port tuple used for exact origin matching."""

    scheme: str
    host: str
    port: int | None = None

    @classmethod
    def parse(cls, value: str | "BrowserOrigin") -> "BrowserOrigin":
        if isinstance(value, cls):
            return value
        raw = str(value or "").strip()
        if not raw:
            raise BrowserScopeError("browser origin must not be empty")
        try:
            parsed = urlsplit(raw)
            scheme = parsed.scheme.casefold()
            host = parsed.hostname
            # Accessing .port validates malformed/non-numeric/out-of-range
            # ports.  Keep this in the try block for a stable policy error.
            port = parsed.port
        except ValueError as exc:
            raise BrowserScopeError(f"invalid browser origin: {value!r}") from exc
        if scheme not in {"http", "https"}:
            raise BrowserScopeError(
                f"browser origin must use http or https (got {parsed.scheme!r})"
            )
        if not parsed.netloc or host is None:
            raise BrowserScopeError("browser origin must include a host")
        if parsed.username is not None or parsed.password is not None:
            raise BrowserScopeError("browser origins may not contain user information")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise BrowserScopeError(
                "browser origin must contain only scheme, host, and optional port"
            )
        canonical_host = _normalise_host(host)
        default_port = _DEFAULT_PORTS[scheme]
        canonical_port = None if port in {None, default_port} else port
        return cls(scheme=scheme, host=canonical_host, port=canonical_port)

    @classmethod
    def from_url(cls, value: str) -> "BrowserOrigin":
        """Parse the origin portion of an absolute HTTP(S) URL."""

        return origin_for_url(value)

    @property
    def value(self) -> str:
        suffix = f":{self.port}" if self.port is not None else ""
        return f"{self.scheme}://{_format_host(self.host)}{suffix}"

    @property
    def origin(self) -> str:
        """Alias used by adapters that call this an origin string."""

        return self.value

    def matches(self, url: str) -> bool:
        try:
            return origin_for_url(url) == self
        except BrowserScopeError:
            return False


def parse_browser_origin(value: str | BrowserOrigin) -> BrowserOrigin:
    """Parse one exact HTTP(S) origin."""

    return BrowserOrigin.parse(value)


def parse_allowed_origins(
    values: Iterable[str | BrowserOrigin] | str | BrowserOrigin,
) -> frozenset[BrowserOrigin]:
    """Canonicalise a configured origin allow-list.

    Strings are treated as one origin, not as an iterable of characters.  An
    empty allow-list is rejected so a QA browser cannot accidentally run with
    an implicit "allow all" policy.
    """

    if isinstance(values, (str, BrowserOrigin)):
        iterable: Iterable[str | BrowserOrigin] = (values,)
    else:
        iterable = values
    result = frozenset(BrowserOrigin.parse(item) for item in iterable)
    if not result:
        raise BrowserScopeError("browser scope requires at least one allowed origin")
    return result


def origin_for_url(url: str) -> BrowserOrigin:
    """Return the canonical origin for an HTTP(S) URL.

    ``file://``, ``data:``, ``javascript:``, ``about:blank`` and other
    browser-local schemes intentionally fail closed here.
    """

    raw = str(url or "").strip()
    if not raw:
        raise BrowserScopeError("browser URL must not be empty")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise BrowserScopeError(f"invalid browser URL: {url!r}") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise BrowserScopeError("browser URL must use an absolute http(s) URL")
    # BrowserOrigin.parse performs the strict userinfo/port/host validation;
    # retaining only scheme + netloc makes URL paths, queries, and fragments
    # irrelevant to origin matching while still checking the authority.
    return BrowserOrigin.parse(f"{parsed.scheme}://{parsed.netloc}")


@dataclass(frozen=True, slots=True)
class BrowserDecision:
    """Non-throwing result from a browser gate check."""

    allowed: bool
    scope: str
    target: str | Path
    reason: str = ""
    origin: BrowserOrigin | None = None


def _safe_identifier(value: str, *, kind: str) -> str:
    identifier = str(value or "").strip()
    if not identifier:
        raise BrowserScopeError(f"{kind} identifier must not be empty")
    if len(identifier) > 240 or any(
        character in identifier for character in "\\/\x00\r\n"
    ):
        raise BrowserScopeError(f"invalid {kind} identifier")
    return identifier


def _new_lane_identifier(lane: BrowserLane, kind: str) -> str:
    return f"aoi-{lane.value}-{kind}-{uuid.uuid4().hex}"


def _iso_utc(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class BrowserAction:
    """Bounded lease metadata for one browser operation."""

    action_id: str
    action: str
    lane: BrowserLane
    run_id: str
    started_at: str
    timeout_seconds: float
    deadline_monotonic: float
    _scope: "BrowserRunScope" = field(repr=False, compare=False)
    _released: bool = field(default=False, repr=False, compare=False)

    @property
    def deadline(self) -> str:
        return (
            _iso_utc(time.time() + max(0.0, self.deadline_monotonic - time.monotonic()))
            or ""
        )

    @property
    def remaining_timeout_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())

    def metadata(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action": self.action,
            "lane": self.lane.value,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "timeout_seconds": self.timeout_seconds,
            "deadline": self.deadline,
            "remaining_timeout_seconds": self.remaining_timeout_seconds,
        }

    def release(self) -> None:
        # BrowserRunScope owns the counter; this method is idempotent even when
        # a caller's cleanup runs twice after cancellation.
        if not self._released:
            object.__setattr__(self, "_released", True)
            self._scope._release_action()

    def __enter__(self) -> "BrowserAction":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()

    async def __aenter__(self) -> "BrowserAction":
        return self

    async def __aexit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


@dataclass
class BrowserRunScope:
    """Per-run browser policy and lifecycle gate.

    ``BrowserRunScope`` deliberately does not own a Playwright context.  It
    only authorises the context an adapter creates.  The default constructors
    make lane separation explicit and generate unrelated profile/process
    identifiers; callers may provide identifiers for tracing, but they may
    not reuse one identifier for both lanes.
    """

    lane: BrowserLane | str
    run_id: str | None = None
    allowed_origins: Sequence[str | BrowserOrigin] | str | BrowserOrigin = ()
    principal: str = "worker"
    agent_run_scope: AgentRunScope | None = None
    profile_id: str | None = None
    process_id: str | None = None
    capability: object | None = field(default=None, repr=False, compare=False)
    action_timeout_seconds: float = _DEFAULT_ACTION_TIMEOUT_SECONDS
    max_action_timeout_seconds: float = _DEFAULT_MAX_ACTION_TIMEOUT_SECONDS
    max_lifetime_seconds: float | None = _DEFAULT_MAX_LIFETIME_SECONDS

    _created_monotonic: float = field(init=False, repr=False)
    _created_epoch: float = field(init=False, repr=False)
    _closed_epoch: float | None = field(init=False, default=None, repr=False)
    _close_reason: str | None = field(init=False, default=None, repr=False)
    _active_actions: int = field(init=False, default=0, repr=False)

    def __post_init__(self) -> None:
        lane = _normalise_lane(self.lane)
        principal = _normalise_principal(self.principal)
        if lane is BrowserLane.DIRECTOR:
            if _agent_team_role_bound():
                raise BrowserLaneViolation(
                    "Agent Team workers cannot acquire the Director browser lane"
                )
            if self.capability is not _DIRECTOR_SCOPE_CAPABILITY:
                raise BrowserLaneViolation(
                    "Director browser capability is parent-owned and cannot be forged"
                )
            if principal not in _DIRECTOR_PRINCIPALS:
                raise BrowserLaneViolation(
                    "only the parent/controller may acquire the Director browser lane"
                )
            origins = parse_allowed_origins(
                self.allowed_origins or (_DEFAULT_DIRECTOR_ORIGIN,)
            )
        else:
            if principal not in _WORKER_PRINCIPALS | _DIRECTOR_PRINCIPALS:
                raise BrowserLaneViolation(
                    f"unknown principal cannot acquire QA browser lane: {principal!r}"
                )
            origins = parse_allowed_origins(self.allowed_origins)
            if any(_is_chatgpt_host(origin.host) for origin in origins):
                raise BrowserOriginViolation(
                    "QA browser may not be configured with a ChatGPT origin"
                )

        if self.agent_run_scope is not None and not isinstance(
            self.agent_run_scope, AgentRunScope
        ):
            raise BrowserScopeError("agent_run_scope must be an AgentRunScope")

        run_id = _safe_identifier(
            self.run_id
            or (
                self.agent_run_scope.run_id
                if self.agent_run_scope
                else uuid.uuid4().hex
            ),
            kind="browser run",
        )
        if self.agent_run_scope is not None and self.agent_run_scope.run_id != run_id:
            raise BrowserScopeError(
                "browser run_id must match the bound AgentRunScope run_id"
            )

        timeout = _positive_finite(
            self.action_timeout_seconds, "action_timeout_seconds"
        )
        max_timeout = _positive_finite(
            self.max_action_timeout_seconds, "max_action_timeout_seconds"
        )
        if timeout > max_timeout:
            raise BrowserScopeError(
                "action_timeout_seconds may not exceed max_action_timeout_seconds"
            )
        lifetime = self.max_lifetime_seconds
        if lifetime is not None:
            lifetime = _positive_finite(lifetime, "max_lifetime_seconds")

        profile_id = _safe_identifier(
            self.profile_id or _new_lane_identifier(lane, "profile"),
            kind="browser profile",
        )
        process_id = _safe_identifier(
            self.process_id or _new_lane_identifier(lane, "process"),
            kind="browser process",
        )
        if profile_id == process_id:
            raise BrowserScopeError(
                "browser profile and process identifiers must differ"
            )
        # A caller must not smuggle a Director profile identifier into a QA
        # run, or vice versa.  Generated IDs naturally satisfy this check.
        if lane is BrowserLane.QA and "director" in profile_id.casefold():
            raise BrowserLaneViolation(
                "QA browser cannot use a Director profile identifier"
            )
        if lane is BrowserLane.QA and "director" in process_id.casefold():
            raise BrowserLaneViolation(
                "QA browser cannot use a Director process identifier"
            )

        object.__setattr__(self, "lane", lane)
        object.__setattr__(self, "principal", principal)
        object.__setattr__(self, "allowed_origins", origins)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "process_id", process_id)
        object.__setattr__(self, "action_timeout_seconds", timeout)
        object.__setattr__(self, "max_action_timeout_seconds", max_timeout)
        object.__setattr__(self, "max_lifetime_seconds", lifetime)
        now = time.time()
        object.__setattr__(self, "_created_epoch", now)
        object.__setattr__(self, "_created_monotonic", time.monotonic())

    @classmethod
    def for_qa(
        cls,
        *,
        run_id: str | None = None,
        allowed_origins: Sequence[str | BrowserOrigin] | str | BrowserOrigin,
        agent_run_scope: AgentRunScope | None = None,
        repository_scope: AgentRunScope | None = None,
        principal: str = "worker",
        profile_id: str | None = None,
        process_id: str | None = None,
        action_timeout_seconds: float = _DEFAULT_ACTION_TIMEOUT_SECONDS,
        max_action_timeout_seconds: float = _DEFAULT_MAX_ACTION_TIMEOUT_SECONDS,
        max_lifetime_seconds: float | None = _DEFAULT_MAX_LIFETIME_SECONDS,
    ) -> "BrowserRunScope":
        """Create an isolated QA scope for one AgentRun."""

        if agent_run_scope is not None and repository_scope is not None:
            raise BrowserScopeError(
                "pass either agent_run_scope or repository_scope, not both"
            )
        bound_scope = agent_run_scope or repository_scope
        return cls(
            lane=BrowserLane.QA,
            run_id=run_id or (bound_scope.run_id if bound_scope else None),
            allowed_origins=allowed_origins,
            principal=principal,
            agent_run_scope=bound_scope,
            profile_id=profile_id,
            process_id=process_id,
            action_timeout_seconds=action_timeout_seconds,
            max_action_timeout_seconds=max_action_timeout_seconds,
            max_lifetime_seconds=max_lifetime_seconds,
        )

    @classmethod
    def for_director(
        cls,
        *,
        run_id: str | None = None,
        principal: str = "parent",
        allowed_origins: Sequence[str | BrowserOrigin] | str | BrowserOrigin = (
            _DEFAULT_DIRECTOR_ORIGIN,
        ),
        profile_id: str | None = None,
        process_id: str | None = None,
        capability: object | None = None,
        action_timeout_seconds: float = _DEFAULT_ACTION_TIMEOUT_SECONDS,
        max_action_timeout_seconds: float = _DEFAULT_MAX_ACTION_TIMEOUT_SECONDS,
        max_lifetime_seconds: float | None = _DEFAULT_MAX_LIFETIME_SECONDS,
    ) -> "BrowserRunScope":
        """Create the parent-owned Director policy (not a worker capability)."""

        return cls(
            lane=BrowserLane.DIRECTOR,
            run_id=run_id,
            allowed_origins=allowed_origins,
            principal=principal,
            profile_id=profile_id,
            process_id=process_id,
            capability=capability,
            action_timeout_seconds=action_timeout_seconds,
            max_action_timeout_seconds=max_action_timeout_seconds,
            max_lifetime_seconds=max_lifetime_seconds,
        )

    @property
    def lane_name(self) -> str:
        return self.lane.value

    @property
    def profile_identifier(self) -> str:
        """Opaque profile identity for trusted orchestration/telemetry."""

        return str(self.profile_id)

    @property
    def process_identifier(self) -> str:
        return str(self.process_id)

    def assert_profile_access(
        self, *, principal: str, lane: BrowserLane | str | None = None
    ) -> str:
        """Return the profile identity only to the lane that owns it.

        This is intentionally an identity gate, not a filesystem path.  A QA
        worker therefore has no API through which it can obtain a Director
        profile directory or its credentials.
        """

        requested_principal = _normalise_principal(principal)
        if (
            self.lane is BrowserLane.DIRECTOR
            and requested_principal not in _DIRECTOR_PRINCIPALS
        ):
            raise BrowserLaneViolation(
                "worker principals cannot access the Director browser profile"
            )
        if lane is not None and _normalise_lane(lane) is not self.lane:
            raise BrowserLaneViolation("browser profile belongs to a different lane")
        return self.profile_identifier

    def _ensure_active(self) -> None:
        if self._closed_epoch is not None:
            detail = f": {self._close_reason}" if self._close_reason else ""
            raise BrowserLifecycleViolation(f"browser run is closed{detail}")
        if (
            self.max_lifetime_seconds is not None
            and time.monotonic() - self._created_monotonic >= self.max_lifetime_seconds
        ):
            object.__setattr__(self, "_closed_epoch", time.time())
            object.__setattr__(
                self, "_close_reason", "maximum browser run lifetime exceeded"
            )
            raise BrowserLifecycleViolation("maximum browser run lifetime exceeded")

    def check_navigation(self, url: str) -> BrowserDecision:
        try:
            self._ensure_active()
            origin = origin_for_url(url)
            if self.lane is BrowserLane.QA and _is_chatgpt_host(origin.host):
                return BrowserDecision(
                    False,
                    "navigation",
                    str(url),
                    "QA browser may not navigate to ChatGPT Web",
                    origin,
                )
            if origin not in self.allowed_origins:
                return BrowserDecision(
                    False,
                    "navigation",
                    str(url),
                    f"origin {origin.value} is not configured for the {self.lane.value} lane",
                    origin,
                )
            return BrowserDecision(True, "navigation", str(url), origin=origin)
        except BrowserLifecycleViolation:
            raise
        except BrowserScopeError as exc:
            return BrowserDecision(False, "navigation", str(url), str(exc))

    def assert_navigation_allowed(self, url: str) -> str:
        decision = self.check_navigation(url)
        if not decision.allowed:
            raise BrowserOriginViolation(
                f"browser navigation denied for {self.lane.value} lane: {decision.reason}"
            )
        return str(decision.target)

    # Adapter-friendly aliases.  Keeping all routes through one implementation
    # makes redirect/request interception less likely to diverge.
    assert_origin_allowed = assert_navigation_allowed
    assert_app_operation_allowed = assert_navigation_allowed

    def _path_scope(self, scope: AgentRunScope | None) -> AgentRunScope:
        bound_scope = scope or self.agent_run_scope or get_current_run_scope()
        if not isinstance(bound_scope, AgentRunScope):
            raise BrowserPathViolation(
                "browser upload/download requires an active AgentRunScope"
            )
        if bound_scope.run_id != self.run_id:
            raise BrowserPathViolation(
                "browser path scope does not match this browser run"
            )
        return bound_scope

    def check_upload(
        self, path: str | Path, *, scope: AgentRunScope | None = None
    ) -> BrowserDecision:
        try:
            self._ensure_active()
            resolved = self._path_scope(scope).assert_read_allowed(path)
            return BrowserDecision(True, "upload", resolved)
        except BrowserLifecycleViolation:
            raise
        except (BrowserScopeError, RunScopeViolation) as exc:
            return BrowserDecision(False, "upload", Path(path), str(exc))

    def assert_upload_allowed(
        self, path: str | Path, *, scope: AgentRunScope | None = None
    ) -> Path:
        decision = self.check_upload(path, scope=scope)
        if not decision.allowed:
            raise BrowserPathViolation(f"browser upload denied: {decision.reason}")
        assert isinstance(decision.target, Path)
        return decision.target

    def check_download(
        self, path: str | Path, *, scope: AgentRunScope | None = None
    ) -> BrowserDecision:
        try:
            self._ensure_active()
            resolved = self._path_scope(scope).assert_mutation_allowed(path, "download")
            return BrowserDecision(True, "download", resolved)
        except BrowserLifecycleViolation:
            raise
        except (BrowserScopeError, RunScopeViolation) as exc:
            return BrowserDecision(False, "download", Path(path), str(exc))

    def assert_download_allowed(
        self, path: str | Path, *, scope: AgentRunScope | None = None
    ) -> Path:
        decision = self.check_download(path, scope=scope)
        if not decision.allowed:
            raise BrowserPathViolation(f"browser download denied: {decision.reason}")
        assert isinstance(decision.target, Path)
        return decision.target

    # Names used by adapters that model browser file chooser directions.
    assert_upload_path_allowed = assert_upload_allowed
    assert_download_path_allowed = assert_download_allowed

    def begin_action(
        self,
        action: str,
        *,
        timeout_seconds: float | None = None,
    ) -> BrowserAction:
        self._ensure_active()
        name = _safe_identifier(action, kind="browser action")
        requested = (
            self.action_timeout_seconds
            if timeout_seconds is None
            else _positive_finite(timeout_seconds, "timeout_seconds")
        )
        timeout = min(requested, self.max_action_timeout_seconds)
        if self.max_lifetime_seconds is not None:
            remaining_lifetime = self.max_lifetime_seconds - (
                time.monotonic() - self._created_monotonic
            )
            if remaining_lifetime <= 0:
                self._ensure_active()
            timeout = min(timeout, remaining_lifetime)
        if timeout <= 0:
            raise BrowserLifecycleViolation(
                "browser action has no remaining time budget"
            )
        object.__setattr__(self, "_active_actions", self._active_actions + 1)
        return BrowserAction(
            action_id=f"{self.lane.value}-{uuid.uuid4().hex}",
            action=name,
            lane=self.lane,
            run_id=str(self.run_id),
            started_at=datetime.now(timezone.utc).isoformat(),
            timeout_seconds=timeout,
            deadline_monotonic=time.monotonic() + timeout,
            _scope=self,
        )

    def _release_action(self) -> None:
        object.__setattr__(self, "_active_actions", max(0, self._active_actions - 1))

    async def run_bounded(
        self,
        awaitable: Awaitable[Any],
        *,
        action: str = "browser-action",
        timeout_seconds: float | None = None,
    ) -> Any:
        """Run one awaitable with the scope's bounded timeout.

        Adapters can still split action/snapshot/wait calls; this helper only
        provides a fail-closed timeout for one small call and never waits
        forever on a hung Playwright/MCP operation.
        """

        if not inspect.isawaitable(awaitable):
            raise TypeError("run_bounded expects an awaitable")
        with self.begin_action(action, timeout_seconds=timeout_seconds) as lease:
            try:
                return await asyncio.wait_for(
                    awaitable, timeout=lease.remaining_timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                raise BrowserLifecycleViolation(
                    f"browser action timed out: {lease.action}"
                ) from exc

    def close(self, reason: str | None = None) -> Mapping[str, Any]:
        if self._closed_epoch is None:
            object.__setattr__(self, "_closed_epoch", time.time())
            object.__setattr__(self, "_close_reason", str(reason or "") or None)
        return self.lifecycle_metadata()

    abort = close

    @property
    def closed(self) -> bool:
        return self._closed_epoch is not None

    def lifecycle_metadata(self) -> dict[str, Any]:
        now_monotonic = time.monotonic()
        if self._closed_epoch is not None:
            state = "closed"
        elif (
            self.max_lifetime_seconds is not None
            and now_monotonic - self._created_monotonic >= self.max_lifetime_seconds
        ):
            state = "expired"
        else:
            state = "active"
        return {
            "lane": self.lane.value,
            "run_id": self.run_id,
            "profile_id": self.profile_identifier,
            "process_id": self.process_identifier,
            "principal": self.principal,
            "state": state,
            "created_at": _iso_utc(self._created_epoch),
            "closed_at": _iso_utc(self._closed_epoch),
            "close_reason": self._close_reason,
            "action_timeout_seconds": self.action_timeout_seconds,
            "max_action_timeout_seconds": self.max_action_timeout_seconds,
            "max_lifetime_seconds": self.max_lifetime_seconds,
            "active_actions": self._active_actions,
            "allowed_origins": sorted(origin.value for origin in self.allowed_origins),
        }

    metadata = lifecycle_metadata
    status = lifecycle_metadata


# Compatibility aliases make the contract easy to discover while allowing
# adapters to use either ``BrowserRunScope`` or the shorter ``BrowserScope``.
BrowserScope = BrowserRunScope
BrowserLaneScope = BrowserRunScope
BrowserOriginScope = BrowserRunScope
DirectorBrowserScope = BrowserRunScope
QABrowserScope = BrowserRunScope


def create_qa_browser_scope(**kwargs: Any) -> BrowserRunScope:
    return BrowserRunScope.for_qa(**kwargs)


def create_director_browser_scope(**kwargs: Any) -> BrowserRunScope:
    return BrowserRunScope.for_director(**kwargs)


def _positive_finite(value: float, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BrowserScopeError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(result) or result <= 0:
        raise BrowserScopeError(f"{name} must be a finite positive number")
    return result


__all__ = [
    "BrowserAction",
    "BrowserDecision",
    "DirectorBrowserScope",
    "BrowserLane",
    "BrowserLaneScope",
    "BrowserLifecycleViolation",
    "BrowserOrigin",
    "BrowserOriginScope",
    "BrowserOriginViolation",
    "BrowserPathViolation",
    "BrowserRunScope",
    "BrowserScope",
    "BrowserScopeError",
    "BrowserLaneViolation",
    "QABrowserScope",
    "create_director_browser_scope",
    "create_qa_browser_scope",
    "origin_for_url",
    "parse_allowed_origins",
    "parse_browser_origin",
]
