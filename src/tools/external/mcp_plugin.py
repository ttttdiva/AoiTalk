"""
MCP (Model Context Protocol) plugin for integrating external tools and data sources
"""
import asyncio
import logging
from typing import Awaitable, Callable, Dict, List, Optional, Any
from contextlib import AsyncExitStack
import json
import os
import inspect
import warnings
from contextvars import ContextVar
from collections.abc import Mapping
from types import SimpleNamespace

from ...utils.subprocess_env import build_aoitalk_subprocess_env
from ...runtime import AsyncResourceScope

from ..core import ToolDefinition, ToolParam
from ...services.outbound_privacy_service import (
    ExternalProviderBlocked,
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
    provider_classification,
)
from ...services.turn_context import get_turn_context

try:
    # The search egress policy is intentionally kept in a small, shared
    # service so every external search route applies the same credential and
    # approved-network checks.  Keep the import optional for older embedders
    # that do not expose the MCP search lane; marked search tools fail closed
    # when the policy cannot be loaded (never silently bypass it).
    from ...services import search_egress_policy
except ImportError:  # pragma: no cover - compatibility with stripped builds
    search_egress_policy = None

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    logging.warning("MCP SDK not available. Install with: pip install mcp[cli]")

logger = logging.getLogger(__name__)
MCP_CLEANUP_TIMEOUT_SECONDS = 15.0


# Plugin-level preflight marks the current call so MCPClient (the lower
# legacy boundary) does not run the same policy twice.  A ContextVar keeps the
# marker isolated when multiple MCP calls run concurrently.
_SEARCH_EGRESS_PREFLIGHT: ContextVar[tuple[str, str] | None] = ContextVar(
    "mcp_search_egress_preflight",
    default=None,
)


# MCP tool descriptions are untrusted provider data.  Search classification
# must therefore come from an explicit metadata marker, never from a tool or
# server name containing words such as ``search``.  A few spellings are
# accepted for interoperability with MCP servers, while arbitrary truthy
# values are deliberately ignored.
_SEARCH_METADATA_KEYS = (
    "search_capability",
    "is_search",
    "search",
    "capability",
    "capabilities",
    "classification",
    "metadata",
    "meta",
    "_meta",
    "annotations",
    "availability",
)
_SEARCH_CAPABILITY_VALUES = frozenset(
    {
        "search",
        "web_search",
        "x_search",
        "public_search",
        "external_search",
        "search_capability",
    }
)


def _explicit_search_marker(value: Any) -> bool | None:
    """Resolve one explicit search marker without name-based inference.

    ``None`` means no marker was supplied.  A trusted server-level marker is
    handled separately by ``mcp_tool_is_search`` and cannot be downgraded by
    provider-supplied tool metadata.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold().replace("-", "_")
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0", "none", ""}:
            return False
        if normalized in _SEARCH_CAPABILITY_VALUES:
            return True
        return None
    if isinstance(value, (list, tuple, set, frozenset)):
        values = [_explicit_search_marker(item) for item in value]
        if any(item is True for item in values):
            return True
        if any(item is False for item in values):
            return False
        return None
    return None


def _search_capability_from_metadata(
    metadata: Any,
    *,
    _seen: set[int] | None = None,
) -> bool | None:
    """Return an explicit MCP search capability marker, if present."""

    if metadata is None:
        return None
    if _seen is None:
        _seen = set()
    marker_id = id(metadata)
    if marker_id in _seen:
        return None
    _seen.add(marker_id)
    if isinstance(metadata, Mapping):
        # A direct marker is authoritative.  ``capability``/``capabilities``
        # are accepted only when their value explicitly names search.
        for key in ("search_capability", "is_search", "search"):
            if key in metadata:
                marker = _explicit_search_marker(metadata.get(key))
                if marker is not None:
                    return marker
        for key in ("capability", "capabilities", "classification"):
            if key in metadata:
                marker = _explicit_search_marker(metadata.get(key))
                if marker is not None:
                    return marker
        # MCP SDKs commonly put vendor metadata under ``meta`` or
        # ``annotations``.  Recursing is bounded to these known containers,
        # not arbitrary values, so a description cannot grant capability.
        for key in ("metadata", "meta", "_meta", "annotations", "availability"):
            if key in metadata:
                marker = _search_capability_from_metadata(
                    metadata.get(key),
                    _seen=_seen,
                )
                if marker is not None:
                    return marker
        return None
    # A provider Tool object may expose metadata through attributes.  Do not
    # inspect ``name`` or ``description``: names are not an authority marker.
    for key in _SEARCH_METADATA_KEYS:
        if hasattr(metadata, key):
            marker = _search_capability_from_metadata(
                {key: getattr(metadata, key)},
                _seen=_seen,
            )
            if marker is not None:
                return marker
    return None


def mcp_tool_is_search(
    tool_metadata: Any = None,
    *,
    server_metadata: Any = None,
) -> bool:
    """Return whether an MCP tool is explicitly classified as search."""

    # Configuration-owned server metadata is authoritative.  A connected MCP
    # process may expose additional tool metadata, but it must not be able to
    # turn off a stricter server-level egress guard by returning ``false``.
    server_marker = _search_capability_from_metadata(server_metadata)
    if server_marker is True:
        return True
    marker = _search_capability_from_metadata(tool_metadata)
    return marker is True


def _metadata_value(metadata: Any, key: str, default: Any = None) -> Any:
    if isinstance(metadata, Mapping):
        return metadata.get(key, default)
    return getattr(metadata, key, default)


def _search_engine_for_metadata(
    tool_metadata: Any,
    *,
    server_name: str,
    _seen: set[int] | None = None,
) -> str:
    """Resolve an explicitly configured search engine for policy checking."""

    if _seen is None:
        _seen = set()
    marker_id = id(tool_metadata)
    if marker_id in _seen:
        return "public"
    _seen.add(marker_id)

    for key in (
        "search_engine",
        "search_provider",
        "provider",
        "engine",
    ):
        value = _metadata_value(tool_metadata, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(tool_metadata, Mapping):
        for key in ("metadata", "meta", "_meta", "availability", "annotations"):
            nested = tool_metadata.get(key)
            if nested is not None:
                resolved = _search_engine_for_metadata(
                    nested,
                    server_name=server_name,
                    _seen=_seen,
                )
                if resolved != "public":
                    return resolved
    else:
        for key in ("metadata", "meta", "_meta", "availability", "annotations"):
            nested = getattr(tool_metadata, key, None)
            if nested is not None:
                resolved = _search_engine_for_metadata(
                    nested,
                    server_name=server_name,
                    _seen=_seen,
                )
                if resolved != "public":
                    return resolved
    # ``public`` is the conservative generic route.  The policy normalizes
    # known hosted/public aliases and can reject an invalid explicit engine;
    # the MCP server name itself is not treated as a capability signal.
    return "public"


def _search_endpoint_for_metadata(
    metadata: Any,
    *,
    _seen: set[int] | None = None,
) -> str:
    """Resolve an explicit search endpoint from tool/server metadata."""

    if metadata is None:
        return ""
    if _seen is None:
        _seen = set()
    marker_id = id(metadata)
    if marker_id in _seen:
        return ""
    _seen.add(marker_id)
    for key in ("base_url", "endpoint", "url", "search_url"):
        value = _metadata_value(metadata, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested_keys = ("metadata", "meta", "_meta", "availability", "annotations")
    if isinstance(metadata, Mapping):
        for key in nested_keys:
            nested = metadata.get(key)
            resolved = _search_endpoint_for_metadata(nested, _seen=_seen)
            if resolved:
                return resolved
    else:
        for key in nested_keys:
            nested = getattr(metadata, key, None)
            resolved = _search_endpoint_for_metadata(nested, _seen=_seen)
            if resolved:
                return resolved
    return ""


_MCP_EGRESS_SCOPE_KEYS = (
    "egress_scope",
    "egressScope",
    "scope",
)
_MCP_ENDPOINT_KEYS = ("base_url", "endpoint", "url", "server_url")


def _mcp_egress_scope(metadata: Any) -> str:
    """Return the operator-owned MCP egress scope (external by default)."""

    if metadata is None:
        return "external"
    if isinstance(metadata, Mapping):
        for key in _MCP_EGRESS_SCOPE_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                normalized = value.strip().casefold().replace("-", "_")
                return "local" if normalized == "local" else "external"
        for key in ("metadata", "meta", "_meta", "availability", "annotations"):
            nested = metadata.get(key)
            resolved = _mcp_egress_scope(nested)
            if resolved == "local":
                return resolved
        return "external"
    for key in _MCP_EGRESS_SCOPE_KEYS:
        value = getattr(metadata, key, None)
        if isinstance(value, str) and value.strip():
            normalized = value.strip().casefold().replace("-", "_")
            return "local" if normalized == "local" else "external"
    for key in ("metadata", "meta", "_meta", "availability", "annotations"):
        nested = getattr(metadata, key, None)
        if _mcp_egress_scope(nested) == "local":
            return "local"
    return "external"


def _mcp_endpoint_for_metadata(metadata: Any) -> str:
    """Resolve an endpoint only from configuration-owned MCP metadata."""

    if metadata is None:
        return ""
    if isinstance(metadata, Mapping):
        for key in _MCP_ENDPOINT_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("metadata", "meta", "_meta", "availability", "annotations"):
            endpoint = _mcp_endpoint_for_metadata(metadata.get(key))
            if endpoint:
                return endpoint
        return ""
    for key in _MCP_ENDPOINT_KEYS:
        value = getattr(metadata, key, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("metadata", "meta", "_meta", "availability", "annotations"):
        endpoint = _mcp_endpoint_for_metadata(getattr(metadata, key, None))
        if endpoint:
            return endpoint
    return ""


def _mcp_operation_route(
    metadata: Any,
    *,
    config: Any,
) -> tuple[str, str, str]:
    """Resolve provider/base URL for one dynamic MCP operation.

    Dynamic MCP operations are external unless an operator explicitly marks
    the server ``egress_scope=local`` *and* supplies a trusted loopback or
    configured local endpoint.  Provider-returned metadata cannot grant the
    local exception because this helper receives server metadata first.
    """

    endpoint = _mcp_endpoint_for_metadata(metadata)
    scope = _mcp_egress_scope(metadata)
    if scope != "local" or not endpoint:
        return "mcp", endpoint, "external"

    try:
        # Use the shared classifier so trusted_local_hosts and loopback
        # handling remain centralized without constructing a second gateway.
        from ...services.outbound_privacy_service import privacy_config

        settings = privacy_config(config)
        classification = provider_classification(
            "openai_compatible_local",
            base_url=endpoint,
            trusted_local_hosts=settings.trusted_local_hosts,
        )
    except Exception:
        classification = "external"
    if classification == "local":
        return "openai_compatible_local", endpoint, "local"
    return "mcp", endpoint, "external"


def _sanitized_search_egress_error(error: BaseException) -> str:
    """Convert policy failures to stable, non-sensitive tool output."""

    code = str(getattr(error, "code", "") or "").strip().casefold()
    if not code:
        code = "egress_unreachable"
    try:
        formatter = getattr(search_egress_policy, "sanitized_precondition_message", None)
        if not callable(formatter):
            formatter = getattr(search_egress_policy, "search_egress_error_message", None)
        if callable(formatter):
            try:
                message = formatter(error)
            except (TypeError, AttributeError):
                message = formatter(code)
            if isinstance(message, str) and message.strip():
                return message.strip()
    except Exception:
        pass
    messages = {
        "credential_missing": "検索の前提条件を満たせません（credential_missing）。検索認証情報を設定してください。",
        "credential_invalid": "検索の認証情報が無効です（credential_invalid）。",
        "provider_invalid": "検索の前提条件を満たせません（provider_invalid）。検索プロバイダ設定を確認してください。",
        "egress_unreachable": "検索エグレスに到達できませんでした（egress_unreachable）。承認済みネットワーク経路を確認してください。",
    }
    return messages.get(
        code,
        "検索の前提条件を満たせません（egress_unreachable）。承認済みネットワーク経路を確認してください。",
    )


def _error_with_code(code: Any) -> RuntimeError:
    """Build a local exception carrying only a stable policy code."""

    normalized = str(code or "egress_unreachable").strip().casefold()
    error = RuntimeError(normalized)
    setattr(error, "code", normalized)
    return error


def _mcp_egress_descriptor(
    *,
    action: str,
    server_name: str,
    provider: str,
    tool: str = "",
    destination: str = "",
    model: str = "",
):
    """Construct the canonical descriptor lazily for optional MCP builds."""

    try:
        from ...services.outbound_privacy_service import EgressDescriptor

        return EgressDescriptor(
            action=action,
            transport="mcp",
            destination=destination or f"mcp://{server_name}",
            provider=provider,
            tool=tool,
            model=model,
        )
    except ImportError:  # pragma: no cover - compatibility with old embeds
        return SimpleNamespace(
            action=action,
            transport="mcp",
            destination=destination or f"mcp://{server_name}",
            provider=provider,
            tool=tool,
            model=model,
        )


def _mcp_gateway(config: Any = None) -> OutboundPrivacyGateway:
    """Resolve one request-scoped gateway carrying the active privacy scope."""

    inherited = get_privacy_policy_context()
    try:
        turn = get_turn_context()
    except Exception:
        turn = None
    return OutboundPrivacyGateway(
        config,
        user_id=str(getattr(turn, "user_id", "") or ""),
        session_id=str(getattr(turn, "session_id", "") or ""),
        session_context=inherited.session_context,
        project_metadata=inherited.project_metadata,
    )


async def _execute_mcp(
    gateway: OutboundPrivacyGateway,
    payload: Any,
    *,
    provider: str,
    descriptor: Any,
    sender: Callable[[Any], Awaitable[Any]],
    base_url: str | None,
    source_kind: str,
    model: str = "",
) -> Any:
    """Run one MCP operation through the gateway exactly once.

    The shipped gateway exposes ``execute``; a mixed-version runtime fails
    closed rather than bypassing the transaction API.
    """

    execute = getattr(gateway, "execute", None)
    if callable(execute):
        return await execute(
            payload,
            provider=provider,
            descriptor=descriptor,
            sender=sender,
            base_url=base_url,
            source_kind=source_kind,
            model=model,
        )

    # Compatibility for older embedded/test gateways.  This bridge is
    # intentionally one-way and deprecated: it accepts only an explicit
    # ``final_payload``/``payload`` from ``protect`` and never falls back to
    # the raw request.  Production OutboundPrivacyGateway exposes ``execute``
    # and always takes the transaction path above.
    protect = getattr(gateway, "protect", None)
    if not callable(protect):
        raise PrivacyError("outbound privacy gateway does not support execution")
    warnings.warn(
        "protect-only MCP privacy gateways are deprecated; implement execute",
        DeprecationWarning,
        stacklevel=2,
    )
    kwargs = {
        "provider": provider,
        "descriptor": descriptor,
        "base_url": base_url,
        "source_kind": source_kind,
        "model": model,
    }
    try:
        protected = protect(payload, **kwargs)
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        kwargs.pop("descriptor", None)
        protected = protect(payload, **kwargs)
    if inspect.isawaitable(protected):
        protected = await protected

    marker = object()
    final_payload = marker
    if isinstance(protected, Mapping):
        final_payload = protected.get("final_payload", marker)
        if final_payload is marker:
            final_payload = protected.get("payload", marker)
    else:
        final_payload = getattr(protected, "final_payload", marker)
        if final_payload is marker:
            final_payload = getattr(protected, "payload", marker)
    if final_payload is marker or final_payload is None:
        raise PrivacyError("legacy privacy protector returned no explicit final payload")
    sent = sender(final_payload)
    if inspect.isawaitable(sent):
        return await sent
    return sent


def _require_reviewed_resource_uri(payload: Any) -> str:
    """Extract the exact URI selected by the egress review transaction.

    ``read_resource`` used to fall back to the caller's original URI when a
    malformed edited payload was supplied.  That turns a review into a
    display-only hint and violates the exact-final-payload invariant, so the
    sender now rejects anything that is not an explicit non-empty mapping
    value.
    """

    if not isinstance(payload, Mapping):
        raise PrivacyError("MCP reviewed resource payload is malformed")
    value = payload.get("uri")
    if not isinstance(value, str) or not value.strip():
        raise PrivacyError("MCP reviewed resource URI is malformed")
    return value


def _policy_decision_error(result: Any) -> str | None:
    """Return a sanitized error for an ambiguous/denied policy result.

    The canonical policy assertions raise on denial and return a truthy
    decision on success.  Compatibility adapters may instead return ``None``
    or an unstructured object; marked search must fail closed in that case.
    """

    if result is True:
        return None
    if result is False or result is None:
        return _sanitized_search_egress_error(
            _error_with_code("egress_unreachable")
        )
    if isinstance(result, Mapping):
        if result.get("allowed") is True:
            return None
        return _sanitized_search_egress_error(
            _error_with_code(result.get("code"))
        )
    if hasattr(result, "allowed"):
        if getattr(result, "allowed") is True:
            return None
        return _sanitized_search_egress_error(
            _error_with_code(getattr(result, "code", "egress_unreachable"))
        )
    return _sanitized_search_egress_error(
        _error_with_code("egress_unreachable")
    )


async def _run_search_egress_precondition(
    config: Any,
    server_name: str,
    tool_name: str,
    metadata: Any,
    *,
    server_metadata: Any = None,
) -> str | None:
    """Run the shared search policy for one explicitly marked MCP call."""

    if not mcp_tool_is_search(metadata, server_metadata=server_metadata):
        return None

    policy = search_egress_policy
    if policy is None:
        return _sanitized_search_egress_error(
            RuntimeError("search egress policy unavailable")
        )

    engine = _search_engine_for_metadata(metadata, server_name=server_name)
    endpoint = _search_endpoint_for_metadata(metadata)
    try:
        # Hosted OpenAI MCP search has a stricter credential requirement;
        # other marked routes use the common approved public-egress gate.
        normalized_engine = engine.strip().casefold().replace("-", "_")
        if normalized_engine in {
            "openai",
            "hosted",
            "hosted_search",
            "openai_hosted",
        }:
            checker = getattr(policy, "assert_openai_hosted_search_ready", None)
            if not callable(checker):
                checker = getattr(policy, "check_search_egress", None)
                if callable(checker):
                    credential_resolver = getattr(
                        policy,
                        "configured_openai_api_key",
                        None,
                    )
                    credential = (
                        credential_resolver(config)
                        if callable(credential_resolver)
                        else None
                    )
                    result = checker(
                        config,
                        "openai",
                        credential=credential,
                        require_credential=True,
                    )
                else:
                    raise RuntimeError("search egress policy unavailable")
            else:
                result = checker(config)
        else:
            checker = getattr(policy, "assert_public_search_egress_approved", None)
            if callable(checker):
                try:
                    result = checker(
                        config,
                        engine=engine,
                        endpoint=endpoint or None,
                    )
                except TypeError:
                    # Compatibility with an older policy that accepted a
                    # positional engine only.
                    result = checker(config, engine)
            else:
                checker = getattr(policy, "check_search_egress", None)
                if not callable(checker):
                    raise RuntimeError("search egress policy unavailable")
                result = checker(config, engine)
        if inspect.isawaitable(result):
            result = await result
        decision_error = _policy_decision_error(result)
        if decision_error:
            return decision_error
    except BaseException as exc:
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        logger.warning(
            "MCP search egress precondition blocked %s.%s (%s)",
            server_name,
            tool_name,
            getattr(exc, "code", type(exc).__name__),
        )
        return _sanitized_search_egress_error(exc)
    return None


def _mcp_tool_params(input_schema: Any) -> List[ToolParam]:
    if not isinstance(input_schema, dict):
        return []

    properties = input_schema.get("properties", {})
    if not isinstance(properties, dict):
        return []

    required = set(input_schema.get("required", []) or [])
    params: List[ToolParam] = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            spec = {}
        param_type = spec.get("type", "string")
        if isinstance(param_type, list):
            concrete_types = [value for value in param_type if value != "null"]
            param_type = concrete_types[0] if concrete_types else "string"
        if param_type not in {"string", "integer", "number", "boolean", "array", "object"}:
            param_type = "string"

        params.append(
            ToolParam(
                name=str(name),
                type=str(param_type),
                description=str(spec.get("description") or ""),
                required=name in required,
                default=spec.get("default"),
                enum=spec.get("enum") if isinstance(spec.get("enum"), list) else None,
            )
        )
    return params


def _format_mcp_content(content: Any) -> str:
    if isinstance(content, list):
        texts = []
        for item in content:
            if hasattr(item, "text"):
                texts.append(str(item.text))
            elif isinstance(item, dict) and "text" in item:
                texts.append(str(item["text"]))
            else:
                texts.append(str(item))
        return "\n".join(texts)
    return str(content)


class MCPClient:
    """
    Model Context Protocol client for connecting to MCP servers
    """

    def __init__(self):
        self.sessions: Dict[str, ClientSession] = {}
        # ``exit_stack`` is retained for compatibility with callers that
        # inspect the client lifecycle.  MCP server resources themselves are
        # owned by their server-specific ``AsyncResourceScope`` instances.
        self.exit_stack: Optional[AsyncExitStack] = None
        self.servers: Dict[str, Dict[str, Any]] = {}
        self._server_scopes: Dict[str, AsyncResourceScope] = {}
        # Search capability metadata is retained separately from the MCP
        # session objects.  Provider-supplied names/descriptions are not
        # authority markers; only explicit metadata copied by MCPPlugin may
        # populate these maps.
        self._server_metadata: Dict[str, Dict[str, Any]] = {}
        self._tool_metadata: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._search_egress_config: Any = None
        self._lifecycle_lock: Optional[asyncio.Lock] = None
        self._server_generation: Dict[str, int] = {}
        self._starting_scopes: Dict[str, AsyncResourceScope] = {}
        self._starting_tasks: Dict[str, asyncio.Task[Any]] = {}
        self._inflight_tasks: Dict[str, set[asyncio.Task[Any]]] = {}
        self._inflight_events: Dict[str, asyncio.Event] = {}
        self._started = False

    def _get_lifecycle_lock(self) -> asyncio.Lock:
        """Return the lock used to serialize server lifecycle operations."""
        # Lazily constructing the lock keeps MCPClient construction usable in
        # synchronous code and binds it to the loop that first uses it.
        if self._lifecycle_lock is None:
            self._lifecycle_lock = asyncio.Lock()
        return self._lifecycle_lock

    async def _acquire_lifecycle_lock_deferring_cancel(
        self,
    ) -> tuple[asyncio.Lock, bool]:
        """Acquire lifecycle state ownership before honoring cancellation."""

        lock = self._get_lifecycle_lock()
        acquire_task = asyncio.create_task(lock.acquire(), name="mcp:lifecycle-lock")
        cancellation_requested = False
        while True:
            try:
                await asyncio.shield(acquire_task)
                break
            except asyncio.CancelledError:
                cancellation_requested = True
                current = asyncio.current_task()
                if current is not None:
                    uncancel = getattr(current, "uncancel", None)
                    if callable(uncancel):
                        uncancel()
        return lock, cancellation_requested

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        """Retrieve a detached transport task's result/exception."""

        if task.cancelled():
            return
        try:
            task.exception()
        except BaseException:
            return

    def _on_inflight_done(self, server_name: str, task: asyncio.Task[Any]) -> None:
        tasks = self._inflight_tasks.get(server_name)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                event = self._inflight_events.get(server_name)
                if event is not None:
                    event.set()
        self._consume_task_result(task)

    def _track_inflight(
        self,
        server_name: str,
        task: asyncio.Task[Any],
    ) -> None:
        tasks = self._inflight_tasks.setdefault(server_name, set())
        tasks.add(task)
        event = self._inflight_events.setdefault(server_name, asyncio.Event())
        event.clear()
        task.add_done_callback(
            lambda finished, name=server_name: self._on_inflight_done(name, finished)
        )

    async def _bounded_await(
        self,
        awaitable: Awaitable[Any],
        *,
        timeout: float,
        description: str,
    ) -> Any:
        """Await setup work without inheriting cancellation-suppression hangs."""

        task = asyncio.ensure_future(awaitable)
        try:
            task.set_name(f"mcp:{description}")
        except (AttributeError, RuntimeError):
            pass
        try:
            done, pending = await asyncio.wait({task}, timeout=timeout)
            if pending:
                task.cancel()
                task.add_done_callback(self._consume_task_result)
                raise asyncio.TimeoutError
            return task.result()
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
                task.add_done_callback(self._consume_task_result)
            raise

    async def _discard_starting_scope(
        self,
        name: str,
        scope: AsyncResourceScope,
    ) -> None:
        async with self._get_lifecycle_lock():
            if self._starting_scopes.get(name) is scope:
                self._starting_scopes.pop(name, None)
            current = asyncio.current_task()
            if self._starting_tasks.get(name) is current:
                self._starting_tasks.pop(name, None)

    async def _session_request(
        self,
        server_name: str,
        operation: Callable[[Any], Awaitable[Any]],
        *,
        timeout: float = 30.0,
    ) -> Any:
        """Run one MCP request without holding the lifecycle lock.

        The session is snapshotted and the transport task is registered while
        the lock is held.  The actual SDK await runs outside the lock so
        ``remove_server``/``stop`` can mark the session closed and perform a
        bounded drain even when a provider ignores cancellation.
        """

        async with self._get_lifecycle_lock():
            session = self.sessions.get(server_name)
            if session is None:
                return None
            task = asyncio.create_task(
                operation(session),
                name=f"mcp:{server_name}:request",
            )
            self._track_inflight(server_name, task)

        try:
            done, pending = await asyncio.wait({task}, timeout=timeout)
            if pending:
                task.cancel()
                raise asyncio.TimeoutError
            return task.result()
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            raise

    async def _drain_inflight(
        self,
        server_name: str,
        tasks: set[asyncio.Task[Any]],
    ) -> None:
        """Wait for in-flight requests, then detach stubborn transports."""

        pending = {task for task in tasks if not task.done()}
        if not pending:
            return
        _done, pending = await asyncio.wait(
            pending,
            timeout=MCP_CLEANUP_TIMEOUT_SECONDS,
        )
        for task in pending:
            task.cancel()
            task.add_done_callback(self._consume_task_result)

    async def _drain_inflight_safe(
        self,
        server_name: str,
        tasks: set[asyncio.Task[Any]],
    ) -> bool:
        """Drain in-flight calls while deferring caller cancellation."""

        drain_task = asyncio.create_task(
            self._drain_inflight(server_name, tasks),
            name=f"mcp-drain:{server_name}",
        )
        cancellation_requested = False
        while not drain_task.done():
            try:
                await asyncio.shield(drain_task)
            except asyncio.CancelledError:
                cancellation_requested = True
                current = asyncio.current_task()
                if current is not None:
                    uncancel = getattr(current, "uncancel", None)
                    if callable(uncancel):
                        uncancel()
        self._consume_task_result(drain_task)
        return cancellation_requested

    async def _drain_tasks_safe(
        self,
        tasks: set[asyncio.Task[Any]],
        description: str,
    ) -> bool:
        """Await canceled lifecycle tasks for a bounded interval."""

        pending = {task for task in tasks if not task.done()}
        if not pending:
            return False
        drain_task = asyncio.create_task(
            asyncio.wait(pending, timeout=MCP_CLEANUP_TIMEOUT_SECONDS),
            name=f"mcp-drain:{description}",
        )
        cancellation_requested = False
        while not drain_task.done():
            try:
                await asyncio.shield(drain_task)
            except asyncio.CancelledError:
                cancellation_requested = True
                current = asyncio.current_task()
                if current is not None:
                    uncancel = getattr(current, "uncancel", None)
                    if callable(uncancel):
                        uncancel()
        try:
            _done, still_pending = drain_task.result()
        except BaseException:
            still_pending = pending
        for task in still_pending:
            task.cancel()
            task.add_done_callback(self._consume_task_result)
        self._consume_task_result(drain_task)
        return cancellation_requested

    async def _cleanup_scope_safe(self, scope: Any, description: str) -> bool:
        """Close one scope through the cancellation-deferring cleanup path."""

        return await self._await_cleanup(scope.aclose(), description)

    async def _await_cleanup(
        self,
        awaitable,
        description: str,
        *,
        raise_errors: bool = False,
    ) -> bool:
        """Await cleanup to completion, even when the caller is cancelled.

        Returns ``True`` when cancellation was requested while waiting.  The
        caller can then re-raise that cancellation after all owned resources
        have been released.  Cleanup failures are logged and intentionally do
        not prevent other resources from being closed.
        """
        cleanup_task = asyncio.create_task(
            awaitable,
            name=f"mcp-cleanup:{description}",
        )
        cancellation_requested = False
        cleanup_error: Exception | None = None
        deadline = asyncio.get_running_loop().time() + MCP_CLEANUP_TIMEOUT_SECONDS

        while not cleanup_task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                cleanup_error = TimeoutError(
                    f"MCP {description} cleanup exceeded its bounded deadline"
                )
                cleanup_task.cancel()
                break
            try:
                await asyncio.wait_for(asyncio.shield(cleanup_task), timeout=remaining)
            except asyncio.CancelledError:
                if cleanup_task.done():
                    # The cleanup task itself may have been cancelled.  Its
                    # result is consumed below; this is not caller
                    # cancellation and should not abort stop/remove.
                    break
                # Do not let cancellation interrupt the owned cleanup task.
                cancellation_requested = True
            except Exception:
                # ``await`` surfaces a cleanup task's exception immediately;
                # break out and retrieve/log it via ``result`` below.
                break

        # A cancellation request or timeout must not leave an unobserved task
        # exception behind.  Do not await a callback that ignored cancellation
        # after the deadline; it is detached and will be consumed when it
        # eventually settles.
        if cleanup_error is not None and not cleanup_task.done():
            cleanup_task.add_done_callback(
                lambda task: task.exception()
                if not task.cancelled()
                else None
            )
        try:
            cleanup_task.result()
        except asyncio.CancelledError:
            # A cleanup callback cancelling itself is consumed here so that
            # stop/remove can continue with the remaining server scopes.
            pass
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = exc
            logger.warning(
                "Error during MCP %s cleanup (%s)",
                description,
                type(exc).__name__,
            )

        if cleanup_error is not None and isinstance(cleanup_error, TimeoutError):
            logger.warning("MCP %s cleanup timed out", description)

        if raise_errors and cleanup_error is not None:
            raise cleanup_error

        return cancellation_requested

    async def start(self):
        """Initialize the MCP client"""
        if not MCP_AVAILABLE:
            logger.error("MCP SDK not available")
            return False

        async with self._get_lifecycle_lock():
            # Starting an already-running client must not replace its exit
            # stack and orphan resources acquired by the first start.
            if self._started and self.exit_stack is not None:
                return True

            self.exit_stack = AsyncExitStack()
            self._started = True
        return True

    async def stop(self):
        """Clean up MCP client resources"""
        # Detach all public state under the lock, then drain/close outside it.
        # Transport cleanup can invoke arbitrary provider code and must not
        # block a concurrent lifecycle transition from acquiring the lock.
        lifecycle_lock, lock_cancellation_requested = (
            await self._acquire_lifecycle_lock_deferring_cancel()
        )
        try:
            server_scopes = list(self._server_scopes.items())
            starting_scopes = list(self._starting_scopes.items())
            starting_tasks = list(self._starting_tasks.values())
            inflight = {
                name: set(tasks)
                for name, tasks in self._inflight_tasks.items()
            }
            # Detach the generation-owned sets before closing.  Late callbacks
            # from a stubborn old transport must never observe or mutate a
            # same-named server that is started during the next lifespan.
            self._inflight_tasks.clear()
            self._inflight_events.clear()
            for name in set(self.sessions) | set(self.servers) | set(server_scopes) | set(starting_scopes):
                self._server_generation[name] = self._server_generation.get(name, 0) + 1
            self._server_scopes.clear()
            self._starting_scopes.clear()
            self._starting_tasks.clear()
            self._server_metadata.clear()
            self._tool_metadata.clear()
            self.sessions.clear()
            self.servers.clear()
            self._started = False
            exit_stack = self.exit_stack
            self.exit_stack = None
        finally:
            lifecycle_lock.release()

        cancellation_requested = lock_cancellation_requested
        current = asyncio.current_task()
        for task in starting_tasks:
            if task is not current and not task.done():
                task.cancel()
        cancellation_requested = (
            await self._drain_tasks_safe(set(starting_tasks), "starting")
            or cancellation_requested
        )
        for name, tasks in inflight.items():
            cancellation_requested = (
                await self._drain_inflight_safe(name, tasks)
                or cancellation_requested
            )

        # A setup that was racing stop is also owned by this lifecycle.  Close
        # each scope once; AsyncResourceScope coalesces duplicate close calls.
        scopes: dict[int, tuple[str, AsyncResourceScope]] = {}
        for name, scope in server_scopes + starting_scopes:
            scopes[id(scope)] = (name, scope)
        for name, scope in scopes.values():
            cancellation_requested = (
                await self._cleanup_scope_safe(scope, f"server:{name}")
                or cancellation_requested
            )

        if exit_stack is not None:
            cancellation_requested = (
                await self._cleanup_scope_safe(exit_stack, "client")
                or cancellation_requested
            )

        if cancellation_requested:
            raise asyncio.CancelledError

    async def add_server(self, name: str, command: str, args: List[str] = None, env: Dict[str, str] = None):
        """
        Add an MCP server

        Args:
            name: Server identifier
            command: Path to server executable
            args: Command line arguments
            env: Environment variables
        """
        if args is None:
            args = []
        # The MCP SDK merges ``env`` with its own small platform allowlist,
        # but this client must not pass the AoiTalk parent environment through
        # as a default.  Only values supplied by the MCP server configuration
        # are explicit child overrides.
        explicit_env = env or {}
        child_env = build_aoitalk_subprocess_env(
            extra_env=explicit_env,
            sensitive_env_keys=explicit_env,
        )

        scope: AsyncResourceScope
        async with self._get_lifecycle_lock():
            if not MCP_AVAILABLE or not self._started or self.exit_stack is None:
                logger.error("MCP client not initialized")
                return False

            # The lock also serializes setup, preventing two concurrent calls
            # from publishing the same name and silently overwriting a live
            # server.  A server is published only after all setup succeeds.
            if (
                name in self.sessions
                or name in self.servers
                or name in self._server_scopes
                or name in self._starting_scopes
            ):
                logger.warning("MCP server '%s' is already active", name)
                return False

            scope = AsyncResourceScope(f"mcp-server:{name}")
            generation = self._server_generation.get(name, 0)
            self._starting_scopes[name] = scope
            current = asyncio.current_task()
            if current is not None:
                self._starting_tasks[name] = current

        try:
            server_params = StdioServerParameters(
                command=command,
                args=args,
                env=child_env,
            )

            # Add bounded timeouts to server startup and initialization.  The
            # helper detaches an SDK awaitable that suppresses cancellation,
            # keeping lifecycle operations finite.
            stdio_client_result = await self._bounded_await(
                scope.enter_async_context(stdio_client(server_params)),
                timeout=45.0,
                description=f"server:{name}:connect",
            )
            read_stream, write_stream = stdio_client_result
            session = await self._bounded_await(
                scope.enter_async_context(ClientSession(read_stream, write_stream)),
                timeout=30.0,
                description=f"server:{name}:session",
            )
            await self._bounded_await(
                session.initialize(),
                timeout=30.0,
                description=f"server:{name}:initialize",
            )
        except asyncio.CancelledError:
            await self._discard_starting_scope(name, scope)
            await self._await_cleanup(scope.aclose(), f"server:{name}")
            raise
        except asyncio.TimeoutError:
            await self._discard_starting_scope(name, scope)
            cancellation_requested = await self._await_cleanup(
                scope.aclose(), f"server:{name}"
            )
            if cancellation_requested:
                raise asyncio.CancelledError
            logger.error(
                "Timeout connecting to MCP server '%s' - server took too long to start",
                name,
            )
            return False
        except Exception as exc:
            await self._discard_starting_scope(name, scope)
            cancellation_requested = await self._await_cleanup(
                scope.aclose(), f"server:{name}"
            )
            if cancellation_requested:
                raise asyncio.CancelledError
            logger.error("Failed to connect to MCP server '%s': %s", name, exc)
            return False

        async with self._get_lifecycle_lock():
            valid = (
                self._started
                and self.exit_stack is not None
                and self._server_generation.get(name, 0) == generation
                and self._starting_scopes.get(name) is scope
            )
            self._starting_scopes.pop(name, None)
            if self._starting_tasks.get(name) is asyncio.current_task():
                self._starting_tasks.pop(name, None)
            if valid:
                self.sessions[name] = session
                self.servers[name] = {
                    "command": command,
                    "args": args,
                    "env": child_env,
                }
                self._server_scopes[name] = scope
        if not valid:
            await self._await_cleanup(scope.aclose(), f"server:{name}")
            return False

        logger.info("MCP server '%s' connected successfully", name)
        return True

    async def remove_server(self, name: str):
        """Remove an MCP server"""
        lifecycle_lock, lock_cancellation_requested = (
            await self._acquire_lifecycle_lock_deferring_cancel()
        )
        try:
            # Remove all projections before close to prevent a concurrent
            # operation from using a session whose transport is being closed.
            scope = self._server_scopes.pop(name, None)
            starting_scope = self._starting_scopes.pop(name, None)
            starting_task = self._starting_tasks.pop(name, None)
            existed = (
                name in self.sessions
                or name in self.servers
                or scope is not None
                or starting_scope is not None
            )
            self._server_generation[name] = self._server_generation.get(name, 0) + 1
            inflight = set(self._inflight_tasks.get(name, set()))
            self._inflight_tasks.pop(name, None)
            self._inflight_events.pop(name, None)
            self.sessions.pop(name, None)
            self.servers.pop(name, None)
            self._server_metadata.pop(name, None)
            for key in tuple(self._tool_metadata):
                if key[0] == name:
                    self._tool_metadata.pop(key, None)
        finally:
            lifecycle_lock.release()

        current = asyncio.current_task()
        if starting_task is not None and starting_task is not current and not starting_task.done():
            starting_task.cancel()
        cancellation_requested = lock_cancellation_requested
        cancellation_requested = (
            await self._drain_tasks_safe(
                {starting_task} if starting_task is not None else set(),
                f"starting:{name}",
            )
            or cancellation_requested
        )
        cancellation_requested = (
            await self._drain_inflight_safe(name, inflight)
            or cancellation_requested
        )
        scopes: dict[int, AsyncResourceScope] = {
            id(item): item for item in (scope, starting_scope) if item is not None
        }
        for item in scopes.values():
            cancellation_requested = (
                await self._cleanup_scope_safe(item, f"server:{name}")
                or cancellation_requested
            )

        if existed:
            logger.info("MCP server '%s' removed", name)
        if cancellation_requested:
            raise asyncio.CancelledError

    async def list_tools(self, server_name: str = None) -> Dict[str, List[Dict]]:
        """
        List available tools from all servers or a specific server

        Args:
            server_name: Optional server name to filter tools

        Returns:
            Dictionary mapping server names to their tool lists
        """
        tools = {}

        async with self._get_lifecycle_lock():
            if server_name:
                servers_to_check = [
                    (server_name, self._server_generation.get(server_name, 0))
                ]
            else:
                servers_to_check = [
                    (name, self._server_generation.get(name, 0))
                    for name in self.sessions.keys()
                ]

        for name, generation in servers_to_check:
            if name not in self.sessions:
                continue

            try:
                response = await self._session_request(
                    name,
                    lambda session: session.list_tools(),
                )
                if response is None:
                    continue
                normalized_tools: list[Dict[str, Any]] = []
                for tool in response.tools:
                    def _tool_value(key: str, default: Any = None) -> Any:
                        if isinstance(tool, Mapping):
                            return tool.get(key, default)
                        return getattr(tool, key, default)

                    # Preserve only the bounded, structured metadata needed
                    # by the caller.  In particular, do not promote a name or
                    # description into a search capability.
                    entry: Dict[str, Any] = {
                        "name": _tool_value("name", ""),
                        "description": _tool_value("description", ""),
                        "inputSchema": _tool_value("inputSchema", None),
                    }
                    for key in (
                        "annotations",
                        "meta",
                        "_meta",
                        "metadata",
                        "search",
                        "capability",
                        "capabilities",
                        "classification",
                        "search_capability",
                        "is_search",
                        "search_engine",
                        "search_provider",
                        "base_url",
                        "endpoint",
                        "url",
                        "search_url",
                        "provider",
                        "engine",
                    ):
                        value = _tool_value(key)
                        if value is not None:
                            entry[key] = value
                    normalized_tools.append(entry)
                async with self._get_lifecycle_lock():
                    # Do not publish metadata from a response belonging to a
                    # removed/replaced server under the same logical name.
                    if (
                        name not in self.sessions
                        or self._server_generation.get(name, 0) != generation
                    ):
                        continue
                    for entry in normalized_tools:
                        tool_name = str(entry.get("name") or "")
                        if tool_name:
                            # Keep a copy for direct ``mcp_tools`` calls that
                            # do not travel through the native ToolDefinition
                            # path.
                            self._tool_metadata[(name, tool_name)] = entry
                    tools[name] = normalized_tools
            except Exception as e:
                logger.error(f"Failed to list tools from server '{name}': {e}")
                tools[name] = []

        return tools

    def set_server_metadata(
        self,
        server_name: str,
        metadata: Any = None,
    ) -> None:
        """Attach explicit, configuration-owned metadata to one MCP server."""

        if isinstance(metadata, Mapping):
            self._server_metadata[server_name] = dict(metadata)
        elif metadata is None:
            self._server_metadata.pop(server_name, None)
        else:
            self._server_metadata[server_name] = {
                "search_capability": metadata,
            }

    def set_search_egress_config(self, config: Any) -> None:
        """Set the config consulted by marked external search calls."""

        self._search_egress_config = config

    def _metadata_for_tool(
        self,
        server_name: str,
        tool_name: str,
    ) -> Dict[str, Any]:
        metadata = dict(self._tool_metadata.get((server_name, tool_name), {}))
        server_metadata = self._server_metadata.get(server_name, {})
        # Server/operator metadata is the authority for the egress boundary.
        # Provider-returned tool metadata is untrusted and must not downgrade
        # an external server to a loopback endpoint or replace its configured
        # engine with a local alias.  It may still supply descriptive fields
        # and non-security hints when the server has not set them.
        authority_keys = {
            "search_capability",
            "is_search",
            "search",
            "capability",
            "capabilities",
            "classification",
            "search_engine",
            "search_provider",
            "provider",
            "engine",
            "base_url",
            "endpoint",
            "url",
            "search_url",
            "egress_scope",
            "egressScope",
            "scope",
            "server_url",
        }
        for key, value in server_metadata.items():
            if key in authority_keys or key not in metadata:
                metadata[key] = value
        if server_metadata and "search_capability" not in metadata:
            metadata["search_capability"] = server_metadata.get("search_capability")
        if mcp_tool_is_search(metadata, server_metadata=server_metadata):
            engine_keys = {
                "search_engine",
                "search_provider",
                "provider",
                "engine",
            }
            endpoint_keys = {"base_url", "endpoint", "url", "search_url"}
            # A marked tool with no operator-owned engine/endpoint must use the
            # conservative public route and cannot claim a provider endpoint
            # solely because the remote MCP server advertised one.
            if not engine_keys.intersection(server_metadata):
                for key in engine_keys:
                    metadata.pop(key, None)
                metadata["search_engine"] = "public"
            if not endpoint_keys.intersection(server_metadata):
                for key in endpoint_keys:
                    metadata.pop(key, None)
        return metadata

    async def _check_search_egress(
        self,
        server_name: str,
        tool_name: str,
        metadata: Any = None,
    ) -> str | None:
        """Run the shared egress precondition for explicitly marked tools."""

        tool_metadata = metadata or self._metadata_for_tool(server_name, tool_name)
        server_metadata = self._server_metadata.get(server_name)
        if not mcp_tool_is_search(tool_metadata, server_metadata=server_metadata):
            return None

        policy = search_egress_policy
        if policy is None:
            return _sanitized_search_egress_error(
                RuntimeError("search egress policy unavailable")
            )

        config = self._search_egress_config
        engine = _search_engine_for_metadata(
            tool_metadata,
            server_name=server_name,
        )
        endpoint = _search_endpoint_for_metadata(tool_metadata)
        try:
            # Hosted OpenAI MCP search has a stricter credential requirement;
            # other marked routes use the common approved public-egress gate.
            normalized_engine = engine.strip().casefold().replace("-", "_")
            if normalized_engine in {
                "openai",
                "hosted",
                "hosted_search",
                "openai_hosted",
            }:
                checker = getattr(policy, "assert_openai_hosted_search_ready", None)
                if not callable(checker):
                    checker = getattr(policy, "check_search_egress", None)
                    if callable(checker):
                        result = checker(
                            config,
                            "openai",
                            credential=getattr(policy, "configured_openai_api_key", lambda _c: None)(config),
                            require_credential=True,
                        )
                    else:
                        raise RuntimeError("search egress policy unavailable")
                else:
                    result = checker(config)
            else:
                checker = getattr(policy, "assert_public_search_egress_approved", None)
                if callable(checker):
                    try:
                        result = checker(
                            config,
                            engine=engine,
                            endpoint=endpoint or None,
                        )
                    except TypeError:
                        # Compatibility with an older policy that accepted a
                        # positional engine only.
                        result = checker(config, engine)
                else:
                    checker = getattr(policy, "check_search_egress", None)
                    if not callable(checker):
                        raise RuntimeError("search egress policy unavailable")
                    result = checker(config, engine)
            if inspect.isawaitable(result):
                result = await result
            decision_error = _policy_decision_error(result)
            if decision_error:
                return decision_error
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            logger.warning(
                "MCP search egress precondition blocked %s.%s (%s)",
                server_name,
                tool_name,
                getattr(exc, "code", type(exc).__name__),
            )
            return _sanitized_search_egress_error(exc)
        return None

    async def call_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> Optional[Dict]:
        """
        Call a tool on a specific MCP server

        Args:
            server_name: Name of the server
            tool_name: Name of the tool to call
            arguments: Tool arguments

        Returns:
            Tool execution result or None if failed
        """
        async with self._get_lifecycle_lock():
            if server_name not in self.sessions:
                logger.error(f"Server '{server_name}' not found")
                return None

        # This is the lowest shared call boundary used by both native MCP
        # definitions and the legacy ``call_mcp_tool`` helper.  Only a tool
        # carrying an explicit search capability marker is subject to the
        # search egress precondition; all unmarked MCP calls retain their
        # historical behavior.
        precondition_error = await self._check_search_egress(
            server_name,
            tool_name,
        )
        if precondition_error:
            return {
                "content": [precondition_error],
                "isError": True,
            }

        metadata = self._metadata_for_tool(server_name, tool_name)
        # Every dynamic MCP tool is an external operation by default.  The
        # only local exception is configuration-owned ``egress_scope=local``
        # pointing at a gateway-trusted endpoint.  Tool/provider metadata is
        # intentionally not sufficient to grant that exception.
        server_metadata = self._server_metadata.get(server_name, {})
        route_metadata = server_metadata if isinstance(server_metadata, Mapping) else {}
        provider, endpoint, _scope = _mcp_operation_route(
            route_metadata,
            config=self._search_egress_config,
        )
        privacy_gateway = _mcp_gateway(self._search_egress_config)
        descriptor = _mcp_egress_descriptor(
            action="mcp.call_tool",
            server_name=server_name,
            provider=provider,
            tool=tool_name,
            destination=endpoint or f"mcp://{server_name}",
        )

        try:
            response = await _execute_mcp(
                privacy_gateway,
                arguments,
                provider=provider,
                descriptor=descriptor,
                base_url=endpoint or None,
                source_kind="mcp_call_tool",
                sender=lambda protected_payload: self._session_request(
                    server_name,
                    lambda session: session.call_tool(
                        tool_name,
                        dict(protected_payload)
                        if isinstance(protected_payload, Mapping)
                        else protected_payload,
                    ),
                ),
            )
            if response is None:
                return None

            response_is_error = bool(getattr(response, "isError", False))
            if response_is_error and mcp_tool_is_search(
                self._metadata_for_tool(server_name, tool_name),
                server_metadata=self._server_metadata.get(server_name),
            ):
                # Search-provider error payloads can contain endpoint details,
                # credentials, or upstream response bodies.  Never return
                # those through the MCP boundary.
                return {
                    "content": [
                        _sanitized_search_egress_error(
                            _error_with_code("egress_unreachable")
                        )
                    ],
                    "isError": True,
                }

            content = response.content
            if privacy_gateway is not None:
                content = privacy_gateway.restore_aliases(content)
            return {
                'content': content,
                'isError': response_is_error
            }

        except asyncio.TimeoutError:
            logger.error(f"Tool '{tool_name}' on server '{server_name}' timed out after 30 seconds")
            if mcp_tool_is_search(
                self._metadata_for_tool(server_name, tool_name),
                server_metadata=self._server_metadata.get(server_name),
            ):
                return {
                    "content": [
                        _sanitized_search_egress_error(
                            _error_with_code("egress_unreachable")
                        )
                    ],
                    "isError": True,
                }
            return {'content': ['Tool execution timed out'], 'isError': True}
        except (ExternalProviderBlocked, PrivacyError):
            # Privacy/local-only denials are stable policy failures, not
            # provider outages.  Keep the MCP surface sanitized and ensure no
            # dynamic call is retried outside the gateway transaction.
            return {
                "content": [
                    "MCPツール呼び出しはプライバシーポリシーにより停止しました。"
                ],
                "isError": True,
            }
        except Exception as e:
            logger.error(
                "Failed to call MCP tool '%s' on server '%s' (%s)",
                tool_name,
                server_name,
                type(e).__name__,
            )
            return None

    async def list_resources(self, server_name: str = None) -> Dict[str, List[Dict]]:
        """
        List available resources from all servers or a specific server

        Args:
            server_name: Optional server name to filter resources

        Returns:
            Dictionary mapping server names to their resource lists
        """
        resources = {}

        async with self._get_lifecycle_lock():
            servers_to_check = [server_name] if server_name else list(self.sessions.keys())

        for name in servers_to_check:
            if name not in self.sessions:
                continue

            try:
                response = await self._session_request(
                    name,
                    lambda session: session.list_resources(),
                )
                if response is None:
                    continue
                resources[name] = [
                    {
                        'uri': resource.uri,
                        'name': resource.name,
                        'description': resource.description,
                        'mimeType': resource.mimeType
                    }
                    for resource in response.resources
                ]
            except Exception as e:
                logger.error(f"Failed to list resources from server '{name}': {e}")
                resources[name] = []

        return resources

    async def read_resource(self, server_name: str, uri: str) -> Optional[Dict]:
        """
        Read a resource from a specific MCP server

        Args:
            server_name: Name of the server
            uri: Resource URI

        Returns:
            Resource content or None if failed
        """
        async with self._get_lifecycle_lock():
            if server_name not in self.sessions:
                logger.error(f"Server '{server_name}' not found")
                return None

        try:
            # Resource reads are dynamic provider calls too.  Treat them as
            # external unless configuration-owned local scope and a trusted
            # endpoint explicitly opt in; provider-returned resource URIs
            # cannot grant local trust.
            server_metadata = self._server_metadata.get(server_name, {})
            route_metadata = (
                server_metadata if isinstance(server_metadata, Mapping) else {}
            )
            provider, endpoint, _scope = _mcp_operation_route(
                route_metadata,
                config=self._search_egress_config,
            )
            gateway = _mcp_gateway(self._search_egress_config)
            descriptor = _mcp_egress_descriptor(
                action="mcp.read_resource",
                server_name=server_name,
                provider=provider,
                destination=endpoint or f"mcp://{server_name}",
            )
            response = await _execute_mcp(
                gateway,
                {"uri": uri},
                provider=provider,
                descriptor=descriptor,
                base_url=endpoint or None,
                source_kind="mcp_read_resource",
                sender=lambda protected_payload: self._session_request(
                    server_name,
                    lambda session: session.read_resource(
                        _require_reviewed_resource_uri(protected_payload)
                    ),
                ),
            )
            if response is None:
                return None

            contents = getattr(response, "contents", None)
            if isinstance(response, Mapping):
                contents = response.get("contents", contents)
            if hasattr(gateway, "restore_aliases"):
                try:
                    contents = gateway.restore_aliases(contents)
                except Exception:
                    # A provider response must not make the resource route
                    # fail after the transport succeeded merely because an
                    # optional alias projection is unavailable.
                    pass
            return {"contents": contents}

        except Exception as exc:
            # URI/provider exception text can contain credentials or user
            # content; keep this background boundary log metadata-only.
            logger.error(
                "Failed to read MCP resource from server '%s' (%s)",
                server_name,
                type(exc).__name__,
            )
            return None

    def get_server_info(self) -> Dict[str, Dict]:
        """Get information about connected servers"""
        return self.servers.copy()

    def is_available(self) -> bool:
        """Check if MCP is available"""
        return MCP_AVAILABLE


class MCPPlugin:
    """
    Plugin wrapper for MCP client functionality
    """

    def __init__(self):
        self.name = "MCP Plugin"
        self.client = MCPClient()
        self._initialized = False
        self._init_loop_id = None
        # Retain the server configuration for runtime search preconditions.
        # This is configuration state only; credentials are never copied into
        # tool metadata or error messages.
        self._config: Any = None

    async def initialize(self, config: Dict[str, Any] = None):
        """Initialize the MCP plugin"""
        if config is None:
            config = {}
        self._config = config
        set_search_config = getattr(self.client, "set_search_egress_config", None)
        if callable(set_search_config):
            set_search_config(config)

        # Track which event loop this was initialized in
        import asyncio
        import platform
        try:
            current_loop = asyncio.get_running_loop()
            self._init_loop_id = id(current_loop)
        except RuntimeError:
            self._init_loop_id = None

        success = await self.client.start()
        if success:
            self._initialized = True

            # Add servers from config if provided
            servers = config.get('servers', {})
            if servers:  # Check if servers is not None and not empty
                for name, server_config in servers.items():
                    # Handle platform-specific configuration
                    shared_env = {}
                    if isinstance(server_config, dict) and ('windows' in server_config or 'linux' in server_config):
                        shared_env = server_config.get('env', {})
                        platform_name = 'windows' if platform.system() == 'Windows' else 'linux'
                        if platform_name in server_config:
                            actual_config = dict(server_config[platform_name])
                        else:
                            logger.warning(f"No configuration for platform '{platform_name}' found for server '{name}'")
                            continue
                    else:
                        actual_config = dict(server_config)

                    # Keep only explicit capability/engine metadata.  The
                    # server name itself is intentionally not interpreted as
                    # a search signal.
                    server_metadata: Dict[str, Any] = {}
                    if isinstance(server_config, Mapping):
                        for key in (
                            "search_capability",
                            "is_search",
                            "search",
                            "capability",
                            "capabilities",
                            "classification",
                            "metadata",
                            "meta",
                            "_meta",
                            "annotations",
                            "search_engine",
                            "search_provider",
                            "base_url",
                            "endpoint",
                            "url",
                            "search_url",
                            "provider",
                            "engine",
                            "egress_scope",
                            "egressScope",
                            "scope",
                            "server_url",
                        ):
                            if key in server_config:
                                server_metadata[key] = server_config[key]
                    if isinstance(actual_config, Mapping):
                        for key in (
                            "search_capability",
                            "is_search",
                            "search",
                            "capability",
                            "capabilities",
                            "classification",
                            "metadata",
                            "meta",
                            "_meta",
                            "annotations",
                            "search_engine",
                            "search_provider",
                            "base_url",
                            "endpoint",
                            "url",
                            "search_url",
                            "provider",
                            "engine",
                            "egress_scope",
                            "egressScope",
                            "scope",
                            "server_url",
                        ):
                            if key in actual_config:
                                server_metadata[key] = actual_config[key]

                    # Resolve only environment values explicitly present in
                    # the server configuration.  The parent environment is
                    # never copied wholesale; ``MCPClient.add_server`` adds
                    # the fixed runtime allowlist and nothing else.
                    env = {**shared_env, **actual_config.get('env', {})}
                    expanded_env = {}
                    for key, value in env.items():
                        if isinstance(value, str) and value.startswith('${') and value.endswith('}'):
                            # Extract env var name and expand it
                            env_var_name = value[2:-1]
                            expanded_value = os.getenv(env_var_name, '')
                            expanded_env[key] = expanded_value
                        else:
                            expanded_env[key] = value

                    try:
                        connected = await self.add_server(
                            name=name,
                            command=actual_config.get('command'),
                            args=actual_config.get('args', []),
                            env=expanded_env,
                        )
                        if connected and server_metadata:
                            self.client.set_server_metadata(name, server_metadata)
                    except asyncio.CancelledError:
                        # If startup is interrupted after one or more servers
                        # connected, release those already-owned scopes before
                        # preserving the caller's cancellation.
                        try:
                            await self.client.stop()
                        finally:
                            self._initialized = False
                        raise

        return success

    async def cleanup(self):
        """Clean up plugin resources"""
        if self._initialized:
            try:
                await self.client.stop()
            finally:
                # Even if the caller is cancelled while a transport is being
                # drained, this plugin instance must not advertise a live
                # client on a subsequent lifecycle attempt.
                self._initialized = False
                self._config = None
                set_search_config = getattr(self.client, "set_search_egress_config", None)
                if callable(set_search_config):
                    set_search_config(None)

    async def add_server(self, name: str, command: str, args: List[str] = None, env: Dict[str, str] = None):
        """Add an MCP server"""
        if not self._initialized:
            logger.error("MCP plugin not initialized")
            return False
        # Keep this boundary explicit as well as the MCPClient boundary so
        # callers that replace/instrument the client cannot accidentally pass
        # the parent process environment to a server.
        explicit_env = env or {}
        child_env = build_aoitalk_subprocess_env(
            extra_env=explicit_env,
            sensitive_env_keys=explicit_env,
        )
        return await self.client.add_server(name, command, args, child_env)

    def is_initialized(self) -> bool:
        """Check if plugin is initialized"""
        return self._initialized

    def is_initialized_in_current_loop(self) -> bool:
        """Check if plugin is initialized in the current event loop"""
        try:
            import asyncio
            current_loop = asyncio.get_running_loop()
            current_loop_id = id(current_loop)
            return self._initialized and self._init_loop_id == current_loop_id
        except RuntimeError:
            # No running loop
            return self._initialized and self._init_loop_id is None

    async def get_tools_for_agent(self) -> List[Dict]:
        """
        Get all available tools formatted for agent use

        Returns:
            List of tool definitions
        """
        if not self._initialized:
            return []

        all_tools = await self.client.list_tools()
        agent_tools = []

        for server_name, tools in all_tools.items():
            for tool in tools:
                merged_tool = self._merge_tool_metadata(server_name, tool)
                self._remember_tool_metadata(server_name, merged_tool)
                search_capability = mcp_tool_is_search(
                    merged_tool,
                    server_metadata=getattr(self.client, "_server_metadata", {}).get(server_name),
                )
                metadata = {
                    key: merged_tool[key]
                    for key in (
                        "search_capability",
                        "is_search",
                        "search",
                        "capability",
                        "capabilities",
                        "classification",
                        "search_engine",
                        "search_provider",
                        "base_url",
                        "endpoint",
                        "url",
                        "search_url",
                        "provider",
                        "engine",
                        "egress_scope",
                        "egressScope",
                        "scope",
                        "server_url",
                    )
                    if key in merged_tool
                }
                if search_capability:
                    metadata["search_capability"] = True
                agent_tools.append({
                    'type': 'function',
                    'function': {
                        'name': f"mcp_{server_name}_{merged_tool['name']}",
                        'description': f"[MCP:{server_name}] {merged_tool['description']}",
                        'parameters': merged_tool['inputSchema']
                    },
                    'server_name': server_name,
                    'tool_name': merged_tool['name'],
                    **metadata,
                })

        return agent_tools

    def _merge_tool_metadata(
        self,
        server_name: str,
        mcp_tool: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Merge server-owned defaults without inferring from names."""

        merged = dict(mcp_tool or {})
        server_metadata = getattr(self.client, "_server_metadata", {}).get(server_name, {})
        for key, value in server_metadata.items():
            merged.setdefault(key, value)
        if server_metadata and "search_capability" not in merged:
            merged["search_capability"] = server_metadata.get("search_capability")
        return merged

    def _remember_tool_metadata(
        self,
        server_name: str,
        mcp_tool: Dict[str, Any],
    ) -> None:
        tool_name = str(mcp_tool.get("name") or "")
        if not tool_name:
            return
        tool_metadata = getattr(self.client, "_tool_metadata", None)
        if not isinstance(tool_metadata, dict):
            tool_metadata = {}
            setattr(self.client, "_tool_metadata", tool_metadata)
        tool_metadata[(server_name, tool_name)] = dict(mcp_tool)

    async def execute_tool(self, tool_call) -> str:
        """
        Execute an MCP tool call

        Args:
            tool_call: Tool call object with name and arguments

        Returns:
            Tool execution result as string
        """
        if not self._initialized:
            return "MCP plugin not initialized"

        tool_name = tool_call.get('name', '')
        if not tool_name.startswith('mcp_'):
            return "Not an MCP tool"

        # Parse server and tool name from function name
        remaining = tool_name[4:]  # Remove 'mcp_' prefix

        # Try to match with known servers
        server_name = None
        actual_tool_name = None

        for known_server in self.client.sessions.keys():
            if remaining.startswith(known_server + '_'):
                server_name = known_server
                actual_tool_name = remaining[len(known_server) + 1:]
                break

        if server_name is None or actual_tool_name is None:
            return f"Invalid MCP tool name format: {tool_name}"
        arguments = tool_call.get('arguments', {})

        # Handle string arguments (from JSON)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return "Invalid tool arguments JSON"

        # Some callers carry the exposed tool metadata alongside a function
        # call.  Remember it before crossing the client boundary so a
        # tool-level marker is enforced even when this legacy path is used.
        supplied_metadata = tool_call.get("metadata") or tool_call.get("tool_definition")
        if supplied_metadata is not None:
            metadata = dict(supplied_metadata) if isinstance(supplied_metadata, Mapping) else {
                "metadata": supplied_metadata,
            }
            metadata.setdefault("name", actual_tool_name)
            self._remember_tool_metadata(server_name, metadata)

        result = await self.client.call_tool(server_name, actual_tool_name, arguments)
        if result is None:
            return f"Failed to execute tool {actual_tool_name} on server {server_name}"

        if result.get('isError', False):
            return f"Tool execution error: {result.get('content', 'Unknown error')}"

        # Format content for return
        content = result.get('content', [])
        if isinstance(content, list):
            texts = []
            for item in content:
                if hasattr(item, 'text'):
                    # TextContent object
                    texts.append(str(item.text))
                elif isinstance(item, dict) and 'text' in item:
                    # Dictionary with 'text' key
                    texts.append(str(item['text']))
                else:
                    # Other types
                    texts.append(str(item))
            return '\n'.join(texts)
        else:
            return str(content)

    def is_available(self) -> bool:
        """Check if MCP is available"""
        return self.client.is_available()

    async def list_tools(self):
        """List MCP tools as native AoiTalk ToolDefinition instances."""
        if not self._initialized:
            return []

        all_tools = await self.client.list_tools()
        tools_list = []

        for server_name, tools in all_tools.items():
            for mcp_tool in tools:
                merged_tool = self._merge_tool_metadata(server_name, mcp_tool)
                self._remember_tool_metadata(server_name, merged_tool)
                tools_list.append(self._to_tool_definition(server_name, merged_tool))

        return tools_list

    def _to_tool_definition(
        self,
        server_name: str,
        mcp_tool: Dict[str, Any],
    ) -> ToolDefinition:
        mcp_tool = self._merge_tool_metadata(server_name, mcp_tool)
        self._remember_tool_metadata(server_name, mcp_tool)
        tool_name = str(mcp_tool.get("name") or "")
        native_name = f"mcp_{server_name}_{tool_name}"
        description = str(mcp_tool.get("description") or tool_name)

        async def _invoke(**kwargs):
            result = await self.client.call_tool(server_name, tool_name, kwargs)
            if result is None:
                return f"Failed to execute tool {tool_name}"
            if result.get("isError", False):
                return f'Tool execution error: {result.get("content", "Unknown error")}'
            return _format_mcp_content(result.get("content", []))

        availability = {
            key: mcp_tool[key]
            for key in (
                "search_capability",
                "is_search",
                "search",
                "capability",
                "capabilities",
                "classification",
                "metadata",
                "meta",
                "_meta",
                "annotations",
                "search_engine",
                "search_provider",
                "base_url",
                "endpoint",
                "url",
                "search_url",
                "provider",
                "engine",
                "egress_scope",
                "egressScope",
                "scope",
                "server_url",
            )
            if key in mcp_tool
        }
        if mcp_tool_is_search(
            mcp_tool,
            server_metadata=getattr(self.client, "_server_metadata", {}).get(server_name),
        ):
            availability["search_capability"] = True

        definition = ToolDefinition(
            name=native_name,
            description=f"[MCP:{server_name}] {description}",
            function=_invoke,
            parameters=_mcp_tool_params(mcp_tool.get("inputSchema")),
            is_async=True,
            side_effect="external",
            timeout_seconds=30.0,
            owner=f"mcp:{server_name}",
            availability=availability or None,
        )
        # ToolDefinition intentionally keeps a stable core schema.  Expose a
        # read-only convenience attribute for integrations that inspect
        # capabilities directly, while retaining the structured availability
        # projection consumed by policy.
        if mcp_tool_is_search(
            mcp_tool,
            server_metadata=getattr(self.client, "_server_metadata", {}).get(server_name),
        ):
            setattr(definition, "search_capability", True)
        return definition

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]):
        """Call an MCP tool by its native `mcp_<server>_<tool>` name."""
        if not self._initialized:
            return "MCP plugin not initialized"
        return await self.execute_tool({"name": tool_name, "arguments": arguments})
