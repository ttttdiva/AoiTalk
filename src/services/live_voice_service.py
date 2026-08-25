"""Live Voice の provider 境界と AoiTalk 側のセッション管理。

このモジュールは、ブラウザへ通常の OpenAI API key や ephemeral secret を
渡さずに Realtime API を利用するための薄い adapter です。ConversationSession、
TurnContext、AgentRun、既存 permission manager を再利用し、音声経路だけの第二
memory や tool registry は作りません。

OpenAI の未公開 GPT-Live API はここでは参照しません。公開されている
Realtime API の unified ``/v1/realtime/calls`` と sideband WebSocket だけを
provider に閉じ込めています。テストでは ``MockRealtimeProvider`` を差し込める
ため、API credential や外部ネットワークを必要としません。
"""

from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import inspect
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    ClassVar,
    Mapping,
    NoReturn,
    Protocol,
)

import httpx

from .agent_run_service import AgentRunService
from ..llm.generation_error import classify_generation_error
from .turn_context import (
    TurnContext,
    reset_turn_context,
    set_turn_context,
)
from .outbound_privacy_service import (
    ExternalProviderBlocked,
    OutboundPrivacyGateway,
    PrivacyReviewDenied,
    RawMediaBlocked,
    get_privacy_policy_context,
    set_privacy_policy_context,
    reset_privacy_policy_context,
)

logger = logging.getLogger(__name__)

LIVE_VOICE_PROVIDER = "openai_realtime"
DEFAULT_REALTIME_MODEL = "gpt-realtime-2.1"
DEFAULT_REALTIME_VOICE = "marin"
# Keep the browser contract deliberately narrower than the arbitrary model and
# voice strings accepted by OpenAI.  Deployments may opt into another current
# Realtime model/voice through the corresponding environment variables, but an
# unknown request value is rejected before any provider call is made.
DEFAULT_REALTIME_MODELS = frozenset(
    {
        # Current Realtime models support the function-calling + audio
        # contract used by the server-side sideband.
        "gpt-realtime-2.1",
        "gpt-realtime-2.1-mini",
        "gpt-realtime-2",
        "gpt-realtime-1.5",
    }
)
DEFAULT_REALTIME_VOICES = frozenset(
    {
        "alloy",
        "ash",
        "ballad",
        "cedar",
        "coral",
        "echo",
        "marin",
        "sage",
        "shimmer",
        "verse",
    }
)
# A conservative built-in surface keeps voice useful without exposing
# destructive tools by default. ``create_task``/``update_task`` remain behind
# the existing external-LLM permission manager; delete/file/command tools are
# not published unless a deployment explicitly opts them in.
DEFAULT_LIVE_VOICE_TOOLS = frozenset(
    {
        "inbox_search_items",
        "docs_search",
        "docs_read",
        "docs_query",
        "list_tasks",
        "create_task",
        "update_task",
    }
)
MAX_TRANSCRIPT_CHARS = 20_000
MAX_EVENT_PAYLOAD_CHARS = 16_000
MAX_TOOL_RESULT_CHARS = 20_000
MAX_SDP_BODY_BYTES = 256 * 1024
MAX_EVENT_BODY_BYTES = 128 * 1024
MAX_BROWSER_TELEMETRY_EVENTS = 256
SIDEBAND_SETUP_TIMEOUT_SECONDS = 10.0
PROVIDER_HANGUP_TIMEOUT_SECONDS = 5.0
MAX_COMPLETED_TOOL_CALLS = 1024
MAX_PENDING_AUDIT_OPERATIONS = 2048
EVENT_SOURCE_BROWSER = "browser"
EVENT_SOURCE_SIDEBAND = "provider_sideband"
MAX_SIDEBAND_EVENT_IDS = 2048
DEFAULT_SESSION_TTL_SECONDS = 60 * 60

# Public browser events are telemetry only.  In particular, no transcript,
# provider response, function call, or tool event is accepted from this route.
BROWSER_TELEMETRY_EVENT_TYPES = frozenset(
    {
        "connect",
        "connected",
        "disconnect",
        "disconnected",
        "error",
        "interrupt",
        "mute",
        "mute.changed",
        "response.created",
        "response.done",
        "session.created",
        "session.updated",
    }
)


class LiveVoiceError(RuntimeError):
    """Live Voice domain error with an HTTP-friendly status code."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class LiveVoiceProviderError(LiveVoiceError):
    """Provider configuration or upstream failure."""


class LiveVoiceNotFoundError(LiveVoiceError):
    """A requested Live Voice session does not exist."""

    def __init__(self, message: str = "Live Voice session not found") -> None:
        super().__init__(message, status_code=404)


class LiveVoicePermissionError(LiveVoiceError):
    """The authenticated actor may not access a session or tool."""

    def __init__(self, message: str = "Live Voice access denied") -> None:
        super().__init__(message, status_code=403)


class EphemeralClientSecret:
    """Deprecated import-compatible stub; ephemeral secrets are unsupported.

    The class intentionally refuses construction so no caller can accidentally
    reintroduce an in-process secret cache through the old public symbol.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise LiveVoiceProviderError(
            "Realtime ephemeral client secrets are no longer supported",
            status_code=410,
        )


def normalize_client_secret(_value: Any) -> NoReturn:
    """Deprecated import-compatible stub; never accepts or returns a secret."""

    raise LiveVoiceProviderError(
        "Realtime ephemeral client secrets are no longer supported",
        status_code=410,
    )


@dataclass(frozen=True)
class LiveVoiceActor:
    """Server-resolved authenticated principal.

    ``user_id`` is intentionally populated only from ``server`` authentication
    state.  Request payloads never participate in actor resolution.
    """

    user_id: str
    username: str = ""
    role: str = "user"
    display_name: str = ""
    # These two fields are populated only by the trusted service boundary when
    # it calls a provider.  ``from_user_info`` never accepts them from a
    # browser payload, and ``to_dict`` intentionally omits them.
    session_id: str = field(default="", repr=False, compare=False)
    project_id: str | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_user_info(cls, user_info: Mapping[str, Any] | None) -> "LiveVoiceActor":
        if not isinstance(user_info, Mapping):
            raise LiveVoicePermissionError("Authenticated AoiTalk actor is required")
        user_id = str(user_info.get("id") or user_info.get("user_id") or "").strip()
        if not user_id:
            raise LiveVoicePermissionError("Authenticated AoiTalk actor is required")
        username = str(user_info.get("username") or "").strip()
        role = str(user_info.get("role") or "user").strip() or "user"
        display_name = str(
            user_info.get("display_name") or username or user_id
        ).strip()
        return cls(
            user_id=user_id,
            username=username,
            role=role,
            display_name=display_name,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.user_id,
            "username": self.username,
            "role": self.role,
            "display_name": self.display_name,
        }

    def with_context(
        self,
        *,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> "LiveVoiceActor":
        """Attach trusted ConversationSession/Project scope for a provider call."""

        return replace(
            self,
            session_id=str(session_id or "").strip(),
            project_id=str(project_id or "").strip() or None,
        )


@dataclass
class LiveVoiceSession:
    """Runtime projection of a durable ConversationSession."""

    id: str
    actor: LiveVoiceActor
    conversation_session_id: str
    provider: str = LIVE_VOICE_PROVIDER
    model: str = DEFAULT_REALTIME_MODEL
    voice: str = DEFAULT_REALTIME_VOICE
    project_id: str | None = None
    character_name: str = "assistant"
    include_project_context: bool | None = None
    # Instructions are server-owned session state.  They are intentionally
    # omitted from the browser snapshot; the /sdp route must never replace
    # them with a client-supplied value.
    _instructions: str | None = field(default=None, repr=False, compare=False)
    # Effective privacy scope is captured at session creation, not read from
    # whichever request happens to connect SDP later.  This keeps a durable
    # Live Voice session bound to its authenticated project/session policy even
    # when the browser reconnects or another request-local context is active.
    session_context: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )
    project_metadata: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )
    privacy_mode: str = "direct"
    status: str = "active"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    agent_run_id: str | None = None
    call_id: str | None = None
    event_count: int = 0
    last_event_type: str | None = None
    turn_context: TurnContext = field(default_factory=TurnContext)
    _seen_event_ids: set[str] = field(default_factory=set, repr=False, compare=False)
    _seen_sideband_event_keys: set[str] = field(
        default_factory=set, repr=False, compare=False
    )
    _seen_browser_event_ids: set[str] = field(
        default_factory=set, repr=False, compare=False
    )
    _browser_event_count: int = field(default=0, repr=False, compare=False)
    _transcript_message_ids: list[str] = field(
        default_factory=list, repr=False, compare=False
    )
    _connect_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
    )
    # Set while the provider call is in flight.  The lifecycle lock is released
    # around that network await so /end and TTL can terminalize the session;
    # the result is then validated against this marker and registry identity
    # before any call/sideband state is installed.
    _connect_in_progress: bool = field(default=False, repr=False, compare=False)
    _tool_call_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
    )
    _tool_call_results: dict[str, dict[str, Any]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _tool_call_inflight: dict[str, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _sideband_provenance: object | None = field(
        default=None, repr=False, compare=False
    )
    mode: str = ""
    policy: Any = field(default=None, repr=False, compare=False)
    voice_generation: Any = field(default=None, repr=False, compare=False)
    _generation_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, repr=False, compare=False
    )
    _character_tts: Any = field(default=None, repr=False, compare=False)
    _defer_assistant_transcript: bool = field(default=False, repr=False, compare=False)
    audio_transport: Any = field(default=None, repr=False, compare=False)
    _sideband_confirmations: list[Any] = field(
        default_factory=list, repr=False, compare=False
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a browser-safe snapshot (never a provider secret)."""

        payload = {
            "id": self.id,
            "live_session_id": self.id,
            "conversation_session_id": self.conversation_session_id,
            # ``session_id`` is included as a convenience for existing WebSocket
            # and chat clients, while the durable name remains explicit above.
            "session_id": self.conversation_session_id,
            "actor": self.actor.to_dict(),
            "provider": self.provider,
            "model": self.model,
            "voice": self.voice,
            "project_id": self.project_id,
            "include_project_context": self.include_project_context,
            "privacy_mode": self.privacy_mode,
            "effective_privacy_mode": self.privacy_mode,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "last_activity_at": self.last_activity_at.isoformat(),
            "agent_run_id": self.agent_run_id,
            "call_id": self.call_id,
            "event_count": self.event_count,
            "last_event_type": self.last_event_type,
        }
        if self.mode:
            payload["mode"] = self.mode
            payload["voice_session_id"] = self.id
        return payload


class LiveVoiceProvider(Protocol):
    """Provider contract used by the service and credential-free tests."""

    name: ClassVar[str]

    async def create_unified_call(
        self,
        *,
        actor: LiveVoiceActor,
        sdp: str,
        model: str,
        voice: str,
        output_modalities: list[str] | None = None,
        instructions: str | None = None,
        tools: list[Mapping[str, Any]] | None = None,
        tool_choice: str | None = None,
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        ...

    async def send_sideband_event(
        self, *, call_id: str, event: Mapping[str, Any]
    ) -> None:
        ...

    async def hangup_call(self, call_id: str) -> None:
        """Terminate a provider-owned realtime call, if one was allocated."""
        ...


def _redact_json(value: Any, *, max_chars: int = MAX_EVENT_PAYLOAD_CHARS) -> Any:
    """Keep audit payloads bounded and remove audio/blob-like fields."""

    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in {
                "audio",
                "delta",
                "base64",
                "audio_bytes",
                "input_audio_buffer",
            }:
                output[str(key)] = "[redacted]"
                continue
            output[str(key)] = _redact_json(item, max_chars=max_chars)
        return output
    if isinstance(value, (list, tuple)):
        return [_redact_json(item, max_chars=max_chars) for item in value[:100]]
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:max_chars]


def _safe_transcript(value: Any) -> str:
    text = str(value or "").replace("\x00", "").strip()
    return text[:MAX_TRANSCRIPT_CHARS]


def _validate_sdp_offer(value: Any) -> str:
    """Apply a small protocol gate before any provider call.

    WebRTC offers are SDP documents and must begin with the RFC 4566
    ``v=0`` version line.  This is deliberately not a full SDP parser; the
    provider remains responsible for semantic negotiation, while the gate
    prevents arbitrary text/blob payloads from crossing the Realtime boundary.
    """

    offer = str(value or "").strip()
    if not offer:
        raise LiveVoiceError("SDP offer is required", status_code=400)
    if len(offer.encode("utf-8")) > MAX_SDP_BODY_BYTES:
        raise LiveVoiceError("SDP offer is too large", status_code=413)
    first_line = offer.splitlines()[0].strip() if offer.splitlines() else ""
    if first_line != "v=0":
        raise LiveVoiceError("Invalid SDP offer", status_code=400)
    return offer


def _extract_transcript(event: Mapping[str, Any]) -> str:
    for key in ("transcript", "text", "content"):
        value = event.get(key)
        if isinstance(value, str):
            return _safe_transcript(value)
    item = event.get("item")
    if isinstance(item, Mapping):
        for key in ("transcript", "text"):
            value = item.get(key)
            if isinstance(value, str):
                return _safe_transcript(value)
        content = item.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, Mapping):
                    for key in ("transcript", "text"):
                        value = part.get(key)
                        if isinstance(value, str):
                            parts.append(value)
            return _safe_transcript("".join(parts))
    return ""


def _restore_transcript_aliases(
    event: Mapping[str, Any], gateway: OutboundPrivacyGateway | None
) -> dict[str, Any]:
    """Restore privacy aliases only in provider-authoritative transcript text.

    Tool arguments and arbitrary provider payloads must remain opaque until
    their normal permission/gateway boundary.  Restoring the whole event here
    would rehydrate an external-egress tool query, so limit restoration to the
    transcript/text fields used by ``_extract_transcript``.
    """

    if gateway is None:
        return dict(event)
    event_type = _event_type(event).lower()
    transcript_event = "transcript" in event_type or "output_text" in event_type
    if not transcript_event:
        return dict(event)

    # Restore only the fields that comprise a provider transcript. Do not walk
    # arbitrary nested mappings: a function/tool argument can legitimately use
    # a ``text`` key and must remain the protected value until its own
    # permission/egress boundary.
    def restore_mapping(
        value: Mapping[str, Any], *, item_scope: bool = False, allow_content: bool = True
    ) -> dict[str, Any]:
        restored: dict[str, Any] = {}
        item_type = str(value.get("type") or "").casefold()
        item_allows_content = allow_content and item_type not in {
            "function_call",
            "tool_call",
        }
        for raw_key, item in value.items():
            key = str(raw_key)
            if (
                key in {"transcript", "text"}
                and isinstance(item, str)
                and (not item_scope or item_type not in {"function_call", "tool_call"})
            ):
                restored[key] = str(gateway.restore(item))
                continue
            if key == "content" and isinstance(item, str) and item_allows_content:
                restored[key] = str(gateway.restore(item))
                continue
            if key == "item" and isinstance(item, Mapping):
                restored[key] = restore_mapping(item, item_scope=True, allow_content=True)
                continue
            if item_scope and key == "content" and isinstance(item, list) and item_allows_content:
                restored[key] = [
                    restore_mapping(part, item_scope=True) if isinstance(part, Mapping) else part
                    for part in item
                ]
                continue
            # Preserve arbitrary provider fields without recursively restoring
            # aliases in nested tool arguments/metadata.
            restored[key] = item
        return restored

    return restore_mapping(event)


def _event_id(event: Mapping[str, Any]) -> str | None:
    raw = event.get("event_id") or event.get("id")
    normalized = str(raw or "").strip()
    return normalized[:256] or None


def _event_key(event: Mapping[str, Any]) -> str:
    """Return a deterministic sideband dedupe key even without event_id."""

    event_id = _event_id(event)
    if event_id:
        return f"id:{event_id}"

    def fingerprint(value: Any) -> Any:
        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            for raw_key, item in value.items():
                key = str(raw_key)
                normalized = key.casefold()
                if normalized in {
                    "audio",
                    "delta",
                    "base64",
                    "audio_bytes",
                    "input_audio_buffer",
                }:
                    # Keep sensitive data out of the dedupe payload while
                    # retaining a digest/length so distinct transcript/audio
                    # deltas do not collapse to one ``[redacted]`` key.
                    rendered = item if isinstance(item, str) else repr(item)
                    output[key] = {
                        "redacted": True,
                        "length": len(rendered),
                        "sha256": hashlib.sha256(rendered.encode("utf-8", errors="replace")).hexdigest(),
                    }
                else:
                    output[key] = fingerprint(item)
            return output
        if isinstance(value, (list, tuple)):
            return [fingerprint(item) for item in value[:100]]
        if isinstance(value, bytes):
            return {
                "redacted": True,
                "length": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
            }
        if isinstance(value, str):
            return value[:MAX_EVENT_PAYLOAD_CHARS]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:MAX_EVENT_PAYLOAD_CHARS]

    try:
        encoded = json.dumps(
            fingerprint(dict(event)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        encoded = repr(sorted((str(key), repr(value)) for key, value in event.items()))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _event_type(event: Mapping[str, Any]) -> str:
    return str(event.get("type") or "").strip()


def _function_call_from_event(event: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    event_type = _event_type(event)
    item = event.get("item")
    item_mapping = item if isinstance(item, Mapping) else {}
    if event_type == "response.output_item.done" and item_mapping.get("type") != "function_call":
        return None
    if event_type not in {
        "response.function_call_arguments.done",
        "response.output_item.done",
    } and item_mapping.get("type") != "function_call":
        return None
    call_id = str(
        event.get("call_id")
        or event.get("item_id")
        or item_mapping.get("call_id")
        or item_mapping.get("id")
        or ""
    ).strip()
    function = event.get("name") or item_mapping.get("name")
    if isinstance(item_mapping.get("function"), Mapping):
        function = function or item_mapping["function"].get("name")
    tool_name = str(function or "").strip()
    raw_args = (
        event.get("arguments")
        or event.get("function_call_arguments")
        or item_mapping.get("arguments")
    )
    if isinstance(item_mapping.get("function"), Mapping):
        raw_args = raw_args or item_mapping["function"].get("arguments")
    if isinstance(raw_args, Mapping):
        arguments = dict(raw_args)
    else:
        try:
            parsed = json.loads(str(raw_args or "{}"))
            arguments = dict(parsed) if isinstance(parsed, Mapping) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            arguments = {}
    if not tool_name:
        return None
    return call_id, tool_name, arguments


class _RealtimeSidebandConnection:
    """One bidirectional provider WebSocket with a serialized writer queue."""

    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket
        self.queue: asyncio.Queue[
            tuple[Mapping[str, Any], asyncio.Future[None]] | None
        ] = asyncio.Queue()
        self.writer_task: asyncio.Task[Any] | None = None
        self.closed = False
        # ``closed`` means no more writes are accepted.  A writer send error
        # sets it before ``close`` gets a chance to run, so it must not be used
        # as the close idempotency guard: the underlying WebSocket still needs
        # to be closed and the writer task joined.  Keep a separate lifecycle
        # flag/lock for that cleanup path.
        self._closing = False
        self._close_complete = False
        self._close_lock = asyncio.Lock()

    async def start(self) -> None:
        self.writer_task = asyncio.create_task(
            self._writer(), name="live-voice-sideband-writer"
        )

    async def _writer(self) -> None:
        try:
            while True:
                item = await self.queue.get()
                if item is None:
                    return
                event, acknowledgement = item
                try:
                    await self.websocket.send(
                        json.dumps(dict(event), ensure_ascii=False, default=str)
                    )
                except Exception as exc:
                    if not acknowledgement.done():
                        acknowledgement.set_exception(exc)
                    raise
                else:
                    if not acknowledgement.done():
                        acknowledgement.set_result(None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            while not self.queue.empty():
                pending = self.queue.get_nowait()
                if pending is None:
                    continue
                _, acknowledgement = pending
                if not acknowledgement.done():
                    acknowledgement.set_exception(exc)
            self.closed = True
            self._drain_pending(exc)
            logger.warning("Live Voice sideband writer disconnected", exc_info=True)

    def _drain_pending(self, error: BaseException) -> None:
        """Fail queued sends when the writer can no longer service them."""

        while True:
            try:
                pending = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if pending is None:
                continue
            _, acknowledgement = pending
            if not acknowledgement.done():
                acknowledgement.set_exception(error)

    async def send(self, event: Mapping[str, Any]) -> None:
        if self.closed:
            raise LiveVoiceProviderError(
                "Realtime sideband is closed", status_code=502
            )
        loop = asyncio.get_running_loop()
        acknowledgement: asyncio.Future[None] = loop.create_future()
        await self.queue.put((dict(event), acknowledgement))
        await acknowledgement

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_complete:
                return
            self._closing = True
            # Reject new sends while retaining the writer task long enough to
            # join it and close the WebSocket even if a previous send failed.
            self.closed = True
            task = self.writer_task
            try:
                if task is not None and task is not asyncio.current_task() and not task.done():
                    await self.queue.put(None)
                    try:
                        await asyncio.wait_for(task, timeout=5.0)
                    except BaseException:
                        task.cancel()
                        try:
                            await task
                        except BaseException:
                            pass
                # A writer that failed before this cleanup may have left a
                # newly-enqueued item behind; fail it rather than hanging the
                # producer awaiting its acknowledgement forever.
                self._drain_pending(
                    LiveVoiceProviderError("Realtime sideband is closed", status_code=502)
                )
            finally:
                try:
                    # Always close the socket, including after a writer send
                    # error (``closed`` is not a sufficient guard here).
                    await self.websocket.close()
                except Exception:
                    pass
                self._close_complete = True
                self._closing = False


class OpenAIRealtimeProvider:
    """公開 OpenAI Realtime API の server-side adapter。"""

    name = LIVE_VOICE_PROVIDER

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        config: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = str(api_key or os.getenv("OPENAI_API_KEY") or "").strip()
        self._base_url = (base_url or "https://api.openai.com").rstrip("/")
        self._privacy_config = config
        self._http_client = http_client
        self._owns_client = http_client is None
        self._timeout = timeout
        self._sideband_connections: dict[str, _RealtimeSidebandConnection] = {}
        # A call's sideband is intentionally single-lifetime.  Once it is
        # closed, reject attempts to create a replacement writer/WebSocket for
        # the same call instead of silently opening one per output event.
        self._closed_sideband_ids: deque[str] = deque(maxlen=1024)
        self._sideband_lock = asyncio.Lock()
        self._privacy_gateways: dict[str, OutboundPrivacyGateway] = {}

    def _require_key(self) -> str:
        if not self._api_key:
            raise LiveVoiceProviderError(
                "OpenAI Realtime provider is not configured",
                status_code=503,
            )
        return self._api_key

    def _safety_identifier(self, actor: LiveVoiceActor) -> str:
        # OpenAI-Safety-Identifier is deliberately pseudonymous; no username or
        # raw database UUID is sent to the provider.
        return hashlib.sha256(actor.user_id.encode("utf-8")).hexdigest()

    async def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self._timeout)
        return self._http_client

    def check_ready(self) -> None:
        """Fail closed before creating local session state when no API key exists."""

        self._require_key()

    async def create_unified_call(
        self,
        *,
        actor: LiveVoiceActor,
        sdp: str,
        model: str,
        voice: str,
        output_modalities: list[str] | None = None,
        instructions: str | None = None,
        tools: list[Mapping[str, Any]] | None = None,
        tool_choice: str | None = None,
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        api_key = self._require_key()
        client = await self._client()
        # The unified WebRTC interface accepts a multipart form containing the
        # raw SDP and a JSON ``session`` field. This matches the current public
        # guide and avoids inventing GPT-Live events.
        modalities = [
            str(item).strip().lower()
            for item in (output_modalities or ["audio"])
            if str(item).strip()
        ]
        if not modalities:
            modalities = ["audio"]
        session_config: dict[str, Any] = {
            "type": "realtime",
            "model": model,
            "output_modalities": modalities,
        }
        # Custom Character TTS consumes Realtime text deltas and must not ask
        # OpenAI to produce a second (unplayed) assistant audio stream.  Keep
        # the native audio voice only for the audio modality.
        if "audio" in modalities:
            session_config["audio"] = {"output": {"voice": voice}}
        if instructions:
            session_config["instructions"] = instructions[:MAX_TRANSCRIPT_CHARS]
        if tools is not None:
            session_config["tools"] = [dict(item) for item in tools]
        if tool_choice is not None:
            session_config["tool_choice"] = tool_choice
        # Keep the provider call auditable without trusting browser supplied
        # actor fields.  The service attaches these values to ``actor`` only
        # after authenticating and resolving the durable session.
        # The actor fields identify the trusted principal; policy metadata is
        # carried separately so privacy_mode/session overrides are evaluated by
        # the gateway without exposing arbitrary browser payload values.
        context_metadata = {
            key: value
            for key, value in {
                "aoi_user_id": actor.user_id,
                "aoi_session_id": getattr(actor, "session_id", ""),
                "aoi_project_id": getattr(actor, "project_id", None),
            }.items()
            if value not in (None, "")
        }
        if context_metadata:
            session_config["metadata"] = context_metadata
        gateway = OutboundPrivacyGateway(
            self._privacy_config,
            user_id=str(actor.user_id or ""),
            session_id=str(getattr(actor, "session_id", "") or ""),
            session_context=(
                dict(session_context) if isinstance(session_context, Mapping) else None
            ),
            project_metadata=(
                dict(project_metadata)
                if isinstance(project_metadata, Mapping)
                else (
                    {"project_id": str(actor.project_id)}
                    if getattr(actor, "project_id", None)
                    else None
                )
            ),
        )
        protected = await gateway.protect(
            {"sdp": str(sdp), "session": session_config},
            provider=LIVE_VOICE_PROVIDER,
            base_url=self._base_url,
            source_kind="live_voice_connect",
        )
        protected_payload = protected.payload
        if isinstance(protected_payload, Mapping):
            sdp = str(protected_payload.get("sdp") or sdp)
            protected_session = protected_payload.get("session")
            if isinstance(protected_session, Mapping):
                session_config = dict(protected_session)
        try:
            response = await client.post(
                f"{self._base_url}/v1/realtime/calls",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "OpenAI-Safety-Identifier": self._safety_identifier(actor),
                },
                files={
                    "sdp": (None, str(sdp), "application/sdp"),
                    "session": (
                        None,
                        json.dumps(session_config, ensure_ascii=False),
                        "application/json",
                    ),
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            failure = classify_generation_error(exc)
            logger.warning(
                "OpenAI Realtime unified call failed: status=%s kind=%s",
                exc.response.status_code,
                failure.kind,
            )
            raise LiveVoiceProviderError(
                _safe_provider_failure_message(exc), status_code=502
            ) from exc
        except httpx.HTTPError as exc:
            failure = classify_generation_error(exc)
            logger.warning(
                "OpenAI Realtime unified call failed: type=%s kind=%s",
                type(exc).__name__,
                failure.kind,
            )
            raise LiveVoiceProviderError(
                _safe_provider_failure_message(exc), status_code=502
            ) from exc
        location = response.headers.get("Location") or response.headers.get("location")
        call_id = str(location or "").rstrip("/").split("/")[-1] or None
        if call_id:
            self._privacy_gateways[call_id] = gateway
        return {
            "sdp": response.text,
            "call_id": call_id,
            "provider": self.name,
            "model": model,
            "session": session_config,
        }

    async def _open_sideband(self, call_id: str) -> _RealtimeSidebandConnection:
        """Get/create the single persistent bidirectional connection for a call."""

        normalized_call_id = str(call_id or "").strip()
        if not normalized_call_id:
            raise LiveVoiceProviderError("Realtime call_id is required", status_code=502)
        async with self._sideband_lock:
            existing = self._sideband_connections.get(normalized_call_id)
            if existing is not None:
                if existing.closed:
                    raise LiveVoiceProviderError(
                        "Realtime sideband is closed", status_code=502
                    )
                return existing
            if normalized_call_id in self._closed_sideband_ids:
                raise LiveVoiceProviderError(
                    "Realtime sideband is closed", status_code=502
                )
            api_key = self._require_key()
            try:
                import websockets
            except ImportError as exc:  # pragma: no cover - optional in tests
                raise LiveVoiceProviderError(
                    "WebSocket provider dependency unavailable", status_code=503
                ) from exc
            url = (
                f"{self._base_url.replace('https://', 'wss://').replace('http://', 'ws://')}"
                f"/v1/realtime?call_id={normalized_call_id}"
            )
            try:
                websocket = await websockets.connect(
                    url,
                    extra_headers={"Authorization": f"Bearer {api_key}"},
                    open_timeout=self._timeout,
                )
            except TypeError:
                websocket = await websockets.connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {api_key}"},
                    open_timeout=self._timeout,
                )
            connection = _RealtimeSidebandConnection(websocket)
            await connection.start()
            self._sideband_connections[normalized_call_id] = connection
            return connection

    async def send_sideband_event(
        self, *, call_id: str, event: Mapping[str, Any]
    ) -> None:
        """Queue an event on the call's one persistent sideband writer."""

        gateway = self._privacy_gateways.get(str(call_id or "").strip())
        outbound = dict(event)
        if gateway is not None:
            event_type = str(outbound.get("type") or "")
            if event_type == "input_audio_buffer.append" and gateway.mode in {
                "protected",
                "local_only",
            }:
                if gateway.settings.raw_media_policy == "block":
                    raise RawMediaBlocked(
                        "raw audio is blocked in protected live voice mode"
                    )
                raise PrivacyReviewDenied(
                    "live voice raw audio requires an explicit review callback"
                )
            protected = await gateway.protect(
                outbound,
                provider=LIVE_VOICE_PROVIDER,
                base_url=self._base_url,
                source_kind="live_voice_sideband",
            )
            outbound = protected.payload
        # Do not open a provider WebSocket until the event has passed the
        # outbound gateway. This is especially important for local_only, where
        # the policy must reject before any external transport is attempted.
        connection = await self._open_sideband(call_id)
        await connection.send(outbound)

    async def close_sideband(self, call_id: str) -> None:
        normalized_call_id = str(call_id or "").strip()
        async with self._sideband_lock:
            connection = self._sideband_connections.pop(normalized_call_id, None)
            if normalized_call_id:
                self._closed_sideband_ids.append(normalized_call_id)
        if connection is not None:
            await connection.close()
        self._privacy_gateways.pop(normalized_call_id, None)

    async def hangup_call(self, call_id: str) -> None:
        """Best-effort provider termination for a unified Realtime call."""

        normalized_call_id = str(call_id or "").strip()
        if not normalized_call_id:
            return
        from urllib.parse import quote

        api_key = self._require_key()
        client = await self._client()
        try:
            response = await client.post(
                f"{self._base_url}/v1/realtime/calls/{quote(normalized_call_id, safe='')}/hangup",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            # A second end request is intentionally idempotent. OpenAI may
            # answer 404 after the provider has already terminated the call.
            if response.status_code != 404:
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            failure = classify_generation_error(exc)
            logger.warning(
                "OpenAI Realtime hangup failed: status=%s kind=%s",
                exc.response.status_code,
                failure.kind,
            )
            raise LiveVoiceProviderError(
                _safe_provider_failure_message(exc), status_code=502
            ) from exc
        except httpx.HTTPError as exc:
            failure = classify_generation_error(exc)
            logger.warning(
                "OpenAI Realtime hangup failed: type=%s kind=%s",
                type(exc).__name__,
                failure.kind,
            )
            raise LiveVoiceProviderError(
                _safe_provider_failure_message(exc), status_code=502
            ) from exc

    def has_sideband(self, call_id: str) -> bool:
        connection = self._sideband_connections.get(str(call_id or "").strip())
        return connection is not None and not connection.closed

    async def iter_sideband_events(self, *, call_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield server events from the provider sideband connection."""
        connection = await self._open_sideband(call_id)
        try:
            async for raw in connection.websocket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(str(raw))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, Mapping):
                    gateway = self._privacy_gateways.get(str(call_id or "").strip())
                    yield _restore_transcript_aliases(parsed, gateway)
        finally:
            await self.close_sideband(call_id)

    def restore_sideband_event(
        self, *, call_id: str, event: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Restore only final transcript aliases for direct test adapters."""

        gateway = self._privacy_gateways.get(str(call_id or "").strip())
        return _restore_transcript_aliases(event, gateway)

    async def close(self) -> None:
        async with self._sideband_lock:
            sidebands = list(self._sideband_connections.values())
            self._sideband_connections.clear()
            self._closed_sideband_ids.clear()
        for connection in sidebands:
            await connection.close()
        self._privacy_gateways.clear()
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None


class MockRealtimeProvider:
    """Deterministic provider used by backend tests and local demos."""

    name = LIVE_VOICE_PROVIDER

    def __init__(self, *, secret_prefix: str = "ek_mock_") -> None:
        # Kept as a zero-call compatibility probe for existing integrations;
        # the unified contract has no client-secret operation.
        self.secret_prefix = secret_prefix
        self.client_secret_requests: list[dict[str, Any]] = []
        self.unified_call_requests: list[dict[str, Any]] = []
        self.sideband_events: list[dict[str, Any]] = []
        self.hangup_calls: list[str] = []

    @staticmethod
    def synthesize_sideband_confirmation(
        event: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        event_type = str(event.get("type") or "")
        if event_type == "conversation.item.delete":
            item_id = str(event.get("item_id") or "").strip()
            if item_id:
                return {"type": "conversation.item.deleted", "item_id": item_id}
        if event_type == "conversation.item.create":
            item = event.get("item")
            if isinstance(item, Mapping):
                item_id = str(item.get("id") or f"mock_item_{uuid.uuid4().hex[:8]}")
                return {
                    "type": "conversation.item.created",
                    "item": {
                        **dict(item),
                        "id": item_id,
                    },
                }
        return None

    def check_ready(self) -> None:
        """Mock provider is always ready and never needs network credentials."""

        return None

    async def create_unified_call(
        self,
        *,
        actor: LiveVoiceActor,
        sdp: str,
        model: str,
        voice: str,
        output_modalities: list[str] | None = None,
        instructions: str | None = None,
        tools: list[Mapping[str, Any]] | None = None,
        tool_choice: str | None = None,
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        call_id = f"rtc_mock_{uuid.uuid4().hex}"
        self.unified_call_requests.append(
            {
                "actor_id": actor.user_id,
                "session_id": getattr(actor, "session_id", ""),
                "project_id": getattr(actor, "project_id", None),
                "sdp": sdp,
                "model": model,
                "voice": voice,
                "output_modalities": list(output_modalities or ["audio"]),
                "instructions": instructions,
                "tools": tools,
                "tool_choice": tool_choice,
                "session_context": dict(session_context or {}),
                "project_metadata": dict(project_metadata or {}),
                "call_id": call_id,
            }
        )
        return {"sdp": "v=0\r\n", "call_id": call_id, "provider": self.name}

    async def send_sideband_event(
        self, *, call_id: str, event: Mapping[str, Any]
    ) -> None:
        self.sideband_events.append({"call_id": call_id, "event": dict(event)})

    async def hangup_call(self, call_id: str) -> None:
        self.hangup_calls.append(str(call_id))


class _NoopAgentRunService:
    """Avoid touching the configured database in credential-free runtimes."""

    async def create_run(self, **_: Any) -> None:
        return None

    async def mark_running(self, *_: Any, **__: Any) -> None:
        return None

    async def record_event(self, *_: Any, **__: Any) -> None:
        return None

    async def record_tool_call(self, *_: Any, **__: Any) -> None:
        return None

    async def complete_run(self, *_: Any, **__: Any) -> None:
        return None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _safe_provider_failure_message(exc: BaseException) -> str:
    """Map provider exceptions to actionable text without echoing upstream data."""

    failure = classify_generation_error(exc)
    # ``classify_generation_error`` intentionally keeps a detailed fallback
    # for normal chat diagnostics. Live Voice must never expose that fallback:
    # an HTTP exception string can include an upstream URL/body or headers.
    if failure.kind != "unknown":
        return failure.user_message
    if isinstance(exc, httpx.TimeoutException):
        return "音声サービスがタイムアウトしました。しばらく待って再試行してください。"
    if isinstance(exc, httpx.TransportError):
        return "音声サービスへ接続できません。ネットワークを確認して再試行してください。"
    return "音声サービスでエラーが発生しました。しばらく待って再試行してください。"


class LiveVoiceService:
    """Session/turn/audit orchestration around one Realtime provider."""

    def __init__(
        self,
        *,
        provider: LiveVoiceProvider | None = None,
        config: Any | None = None,
        db_manager: Any | None = None,
        agent_run_service: AgentRunService | None = None,
        repository_factory: Callable[[], Any] | None = None,
        permission_checker: Callable[..., Awaitable[bool] | bool] | None = None,
        tool_executor: Callable[..., Awaitable[Any] | Any] | None = None,
        broadcaster: Callable[..., Awaitable[Any] | Any] | None = None,
        allowed_tools: set[str] | list[str] | tuple[str, ...] | None = None,
        allowed_models: set[str] | list[str] | tuple[str, ...] | None = None,
        allowed_voices: set[str] | list[str] | tuple[str, ...] | None = None,
        session_ttl_seconds: float | None = None,
    ) -> None:
        self.provider: LiveVoiceProvider = provider or OpenAIRealtimeProvider()
        self.config = config if config is not None else getattr(self.provider, "_privacy_config", None)
        self.db_manager = db_manager
        self.agent_runs = agent_run_service or (
            AgentRunService(db_manager)
            if db_manager is not None
            else _NoopAgentRunService()
        )
        self._agent_run_persistence_required = (
            db_manager is not None or agent_run_service is not None
        )
        self.repository_factory = repository_factory
        self.permission_checker = permission_checker
        self.tool_executor = tool_executor
        self.broadcaster = broadcaster
        self._sessions: dict[str, LiveVoiceSession] = {}
        self._sideband_tasks: dict[str, asyncio.Task[Any]] = {}
        self._sideband_processor_tasks: dict[str, asyncio.Task[Any]] = {}
        self._sideband_event_queues: dict[str, asyncio.Queue[Any | None]] = {}
        self._connect_cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()
        self._allowed_tools = self._normalize_allowlist(
            allowed_tools,
            env_name=("AOITALK_LIVE_VOICE_ALLOWED_TOOLS", "AOITALK_LIVE_VOICE_TOOLS"),
            default=DEFAULT_LIVE_VOICE_TOOLS,
        )
        self._allowed_models = self._normalize_allowlist(
            allowed_models,
            env_name=("AOITALK_LIVE_VOICE_MODELS",),
            default=DEFAULT_REALTIME_MODELS,
        )
        self._allowed_voices = self._normalize_allowlist(
            allowed_voices,
            env_name=("AOITALK_LIVE_VOICE_VOICES",),
            default=DEFAULT_REALTIME_VOICES,
        )
        ttl_value = session_ttl_seconds
        if ttl_value is None:
            ttl_value = os.getenv("AOITALK_LIVE_VOICE_TTL_SECONDS")
        try:
            self.session_ttl_seconds = max(0.1, float(ttl_value or DEFAULT_SESSION_TTL_SECONDS))
        except (TypeError, ValueError):
            self.session_ttl_seconds = float(DEFAULT_SESSION_TTL_SECONDS)
        max_actor_value = os.getenv("AOITALK_LIVE_VOICE_MAX_SESSIONS_PER_ACTOR", "1")
        try:
            self.max_sessions_per_actor = max(1, int(max_actor_value))
        except (TypeError, ValueError):
            self.max_sessions_per_actor = 1
        try:
            self.start_failure_limit = max(
                1, int(os.getenv("AOITALK_LIVE_VOICE_START_FAILURE_LIMIT", "5"))
            )
        except (TypeError, ValueError):
            self.start_failure_limit = 5
        try:
            self.start_failure_window_seconds = max(
                1.0,
                float(
                    os.getenv("AOITALK_LIVE_VOICE_START_FAILURE_WINDOW_SECONDS", "60")
                ),
            )
        except (TypeError, ValueError):
            self.start_failure_window_seconds = 60.0
        self._cleanup_task: asyncio.Task[Any] | None = None
        self._voice_tool_definitions: dict[str, Any] | None = None
        self._starting_actors: set[str] = set()
        self._start_failures: dict[str, list[float]] = {}
        # Terminal AgentRun updates are retried out-of-band when a configured
        # repository is temporarily unavailable. Runtime cleanup is never
        # blocked on this ledger, and callers receive ``audit_pending``
        # instead of a false durable-success signal.
        self._pending_terminalizations: dict[str, dict[str, Any]] = {}
        self._terminalization_retry_task: asyncio.Task[Any] | None = None
        self._pending_tool_audits: dict[str, dict[str, Any]] = {}
        self._tool_audit_retry_task: asyncio.Task[Any] | None = None
        self._pending_run_events: dict[str, dict[str, Any]] = {}
        self._run_event_retry_task: asyncio.Task[Any] | None = None
        self._voice_session_service: Any | None = None

    def voice_session_service(self) -> Any:
        if self._voice_session_service is None:
            from .voice_sessions.service import VoiceSessionService

            self._voice_session_service = VoiceSessionService(
                self,
                config=self.config,
                server=getattr(self, "_server", None),
            )
        return self._voice_session_service

    @staticmethod
    def _normalize_allowlist(
        values: set[str] | list[str] | tuple[str, ...] | None,
        *,
        env_name: tuple[str, ...],
        default: set[str] | frozenset[str] | None = None,
    ) -> frozenset[str]:
        if values is None:
            for name in env_name:
                raw = os.getenv(name)
                if raw is not None:
                    values = [item.strip() for item in raw.split(",") if item.strip()]
                    break
        if values is None:
            values = default or ()
        return frozenset(str(item).strip() for item in values if str(item).strip())

    @classmethod
    def from_server(cls, server: Any) -> "LiveVoiceService":
        existing = getattr(server, "live_voice_service", None)
        if isinstance(existing, cls):
            return existing
        provider = getattr(server, "live_voice_provider", None)
        if provider is None and str(os.getenv("AOITALK_LIVE_VOICE_PROVIDER") or "").casefold() == "mock":
            provider = MockRealtimeProvider()
        if provider is None:
            provider = OpenAIRealtimeProvider(config=getattr(server, "config", None))

        async def _broadcast(payload: Mapping[str, Any], *, session_id: str, user_id: str) -> None:
            manager = getattr(server, "manager", None)
            if manager is None or not hasattr(manager, "broadcast"):
                return
            await manager.broadcast(dict(payload), session_id=session_id, user_id=user_id)

        service = cls(
            provider=provider,
            config=getattr(server, "config", None),
            db_manager=getattr(server, "_db_manager", None),
            broadcaster=_broadcast,
        )
        service._server = server
        try:
            server.live_voice_service = service
        except Exception:
            pass
        service._server = server
        return service

    def _repo(self) -> Any | None:
        if self.repository_factory is not None:
            try:
                return self.repository_factory()
            except Exception as exc:
                # Treat repository construction failures exactly like a
                # missing configured repository. Callers then fail closed with
                # a 503 instead of leaking a raw factory exception as HTTP
                # 500 or falling back to in-memory persistence.
                logger.warning(
                    "Live Voice repository construction failed: %s", type(exc).__name__
                )
                return None
        if self.db_manager is None:
            return None
        try:
            from ..memory.conversation_repository import ConversationRepository

            return ConversationRepository()
        except Exception:
            return None

    def _canonical_voice_tool_map(self) -> dict[str, Any]:
        """Resolve the existing canonical definitions used by AoiTalk agents.

        The global registry is consulted first.  The runtime's direct Docs and
        project-task factories are then loaded lazily for deployments where
        the global registry has not yet been initialized; no Live Voice-only
        function implementations or registry are created.
        """

        if self._voice_tool_definitions is not None:
            return self._voice_tool_definitions
        definitions: dict[str, Any] = {}
        try:
            from ..tools.registry import get_registry

            registry = get_registry()
            for name in registry.get_names():
                definition = registry.get(name)
                if definition is not None:
                    definitions[str(name)] = definition
        except Exception:
            pass
        try:
            from ..tools.core import ensure_tool_definitions
            from ..tools.docs_direct import build_docs_direct_tools

            for definition in ensure_tool_definitions(build_docs_direct_tools()):
                definitions.setdefault(definition.name, definition)
        except Exception as exc:
            logger.debug("Live Voice Docs tool definitions unavailable: %s", type(exc).__name__)
        try:
            from ..agents.project_management.task_tools import build_task_tools

            for definition in build_task_tools():
                definitions.setdefault(str(definition.name), definition)
        except Exception as exc:
            logger.debug("Live Voice task tool definitions unavailable: %s", type(exc).__name__)
        self._voice_tool_definitions = definitions
        return definitions

    def _tool_definitions(self) -> list[dict[str, Any]]:
        """Serialize only the explicitly allowlisted canonical tools."""

        if not self._allowed_tools:
            return []
        tool_map = self._canonical_voice_tool_map()
        definitions: list[dict[str, Any]] = []
        for name in sorted(self._allowed_tools):
            definition = tool_map.get(name)
            if definition is None:
                continue
            try:
                definitions.append(
                    {
                        "type": "function",
                        "name": definition.name,
                        "description": str(definition.description or "")[:4000],
                        "parameters": definition.to_json_schema(),
                    }
                )
            except Exception:
                continue
        return definitions

    async def _call_provider(
        self,
        method_name: str,
        *,
        actor: LiveVoiceActor,
        session: LiveVoiceSession,
        **kwargs: Any,
    ) -> Any:
        """Call current provider contract while tolerating old test adapters."""

        method = getattr(self.provider, method_name)
        # Bind the durable session scope for the full provider call.  Realtime
        # connect requests are separate HTTP tasks and therefore cannot rely on
        # the contextvar that was active during /session/start.
        privacy_token = set_privacy_policy_context(
            session_context=session.session_context,
            project_metadata=session.project_metadata,
        )
        try:
            provider_kwargs = dict(kwargs)
            provider_kwargs.setdefault("session_context", session.session_context)
            provider_kwargs.setdefault("project_metadata", session.project_metadata)
            # Filter compatibility kwargs from the signature *before* the
            # provider call. Retrying after an arbitrary TypeError can duplicate
            # an already accepted provider call (for example, a provider may
            # allocate a call and then raise internally).
            try:
                signature = inspect.signature(method)
            except (TypeError, ValueError):
                signature = None
            if signature is not None:
                accepts_kwargs = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
                if not accepts_kwargs:
                    accepted = {
                        name
                        for name, parameter in signature.parameters.items()
                        if parameter.kind
                        in {
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                            inspect.Parameter.KEYWORD_ONLY,
                        }
                    }
                    provider_kwargs = {
                        key: value
                        for key, value in provider_kwargs.items()
                        if key in accepted
                    }
            return await _maybe_await(method(actor=actor, **provider_kwargs))
        finally:
            reset_privacy_policy_context(privacy_token)

    def _provider_actor(
        self,
        actor: LiveVoiceActor,
        session: LiveVoiceSession,
    ) -> LiveVoiceActor:
        """Return an auth-preserving actor enriched with trusted scope."""

        return actor.with_context(
            session_id=session.conversation_session_id,
            project_id=session.project_id,
        )

    async def _load_project_privacy_metadata(
        self, project_id: str | None
    ) -> dict[str, Any]:
        """Load persisted Project metadata before any Realtime egress.

        Browser payloads carry only a project identifier.  The privacy mode is
        server-owned metadata, so resolve it from the canonical repository when
        a database is configured; never infer a weaker mode from the payload.
        Credential-free/mock services intentionally have no repository and
        retain the inherited/global policy.
        """

        normalized = str(project_id or "").strip()
        if not normalized or self.db_manager is None:
            return {}
        try:
            from uuid import UUID

            from ..memory.project_repository import ProjectRepository

            db_session = await _maybe_await(self.db_manager.get_session())
            try:
                project = await ProjectRepository.get_by_id(
                    db_session, UUID(normalized), include_members=False
                )
            finally:
                close = getattr(db_session, "close", None)
                if close is not None:
                    await _maybe_await(close())
            metadata = getattr(project, "project_metadata", None)
            return dict(metadata) if isinstance(metadata, Mapping) else {}
        except (TypeError, ValueError):
            # Invalid/ephemeral project identifiers are still represented in
            # project_metadata below, but cannot be used for a DB lookup.
            return {}
        except Exception as exc:
            logger.warning(
                "Live Voice Project privacy metadata lookup failed: %s",
                type(exc).__name__,
            )
            if self.db_manager is not None:
                raise LiveVoiceProviderError(
                    "Live Voice privacy scope is unavailable", status_code=503
                ) from exc
            return {}

    async def _resolve_privacy_scope(
        self,
        *,
        project_id: str | None,
        durable_session: Any | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Snapshot inherited, ConversationSession, and Project policy scope."""

        inherited = get_privacy_policy_context()
        session_context: dict[str, Any] = dict(inherited.session_context or {})
        project_metadata: dict[str, Any] = dict(inherited.project_metadata or {})

        try:
            durable_context = getattr(durable_session, "context", None)
        except Exception as exc:
            # ConversationRepository may return a detached ORM row.  Privacy
            # scope is reloaded from canonical IDs below; never let a lazy
            # relationship access turn a valid native voice start into a 500.
            logger.debug(
                "Live Voice detached ConversationSession context skipped: %s",
                type(exc).__name__,
            )
            durable_context = None
        if isinstance(durable_context, Mapping):
            session_context.update(dict(durable_context))

        normalized_project_id = str(project_id or "").strip() or None
        if normalized_project_id:
            # Resolve persisted metadata even when a relationship was not
            # eagerly loaded by ConversationRepository.
            loaded = await self._load_project_privacy_metadata(normalized_project_id)
            if loaded:
                project_metadata.update(loaded)
            project_metadata.setdefault("project_id", normalized_project_id)

        return session_context, project_metadata

    def _privacy_preflight(
        self,
        actor: LiveVoiceActor,
        *,
        session_id: str | None = None,
        project_id: str | None = None,
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Reject an external Realtime route under effective local_only mode."""

        provider_name = str(getattr(self.provider, "name", LIVE_VOICE_PROVIDER) or LIVE_VOICE_PROVIDER)
        base_url = str(getattr(self.provider, "_base_url", "") or "")
        gateway = OutboundPrivacyGateway(
            self.config,
            user_id=actor.user_id,
            session_id=str(session_id or ""),
            session_context=(
                dict(session_context) if isinstance(session_context, Mapping) else None
            ),
            project_metadata=(
                dict(project_metadata)
                if isinstance(project_metadata, Mapping)
                else ({"project_id": str(project_id)} if project_id else None)
            ),
        )
        try:
            gateway.ensure_provider_allowed(provider_name, base_url=base_url)
        except ExternalProviderBlocked as exc:
            raise LiveVoiceError(
                "Live Voice is blocked by local_only privacy mode",
                status_code=403,
            ) from exc
        return gateway.mode

    def _session_expired(self, session: LiveVoiceSession) -> bool:
        return (
            self.session_ttl_seconds > 0
            and (
                datetime.now(timezone.utc) - session.last_activity_at
            ).total_seconds()
            >= self.session_ttl_seconds
        )

    async def _ensure_cleanup_task(self) -> None:
        task = self._cleanup_task
        if task is None or task.done():
            self._cleanup_task = asyncio.create_task(
                self._expiry_loop(), name="live-voice-session-cleanup"
            )

    async def _expiry_loop(self) -> None:
        interval = min(60.0, max(1.0, self.session_ttl_seconds / 4.0))
        try:
            while True:
                await asyncio.sleep(interval)
                await self._expire_sessions()
        except asyncio.CancelledError:
            raise

    async def _cancel_task(self, task: asyncio.Task[Any] | None) -> None:
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _queue_terminalization(
        self,
        run_id: str | None,
        *,
        result: Mapping[str, Any] | None,
        message: str,
    ) -> None:
        normalized = str(run_id or "").strip()
        if not normalized or not self._agent_run_persistence_required:
            return
        async with self._lock:
            self._pending_terminalizations[normalized] = {
                "result": dict(result or {}),
                "message": message,
                "attempts": 0,
                "next_at": time.monotonic() + 0.1,
            }
            task = self._terminalization_retry_task
            if task is None or task.done():
                self._terminalization_retry_task = asyncio.create_task(
                    self._retry_terminalizations(),
                    name="live-voice-agent-run-terminalization-retry",
                )

    async def _complete_agent_run(
        self,
        run_id: str | None,
        *,
        result: Mapping[str, Any] | None,
        message: str,
    ) -> bool:
        """Complete a run, retaining a durable retry intent on failure."""

        if not run_id or not self._agent_run_persistence_required:
            return True
        try:
            completed = await self.agent_runs.complete_run(
                run_id,
                result=dict(result or {}),
                message=message,
            )
            if completed is None:
                raise RuntimeError("AgentRun completion returned no durable row")
            return True
        except Exception as exc:
            logger.warning("Live Voice AgentRun completion pending: %s", type(exc).__name__)
            await self._queue_terminalization(
                run_id,
                result=result,
                message=message,
            )
            return False

    async def _retry_terminalizations(self) -> None:
        try:
            while True:
                async with self._lock:
                    pending = list(self._pending_terminalizations.items())
                if not pending:
                    return
                now = time.monotonic()
                for run_id, item in pending:
                    if float(item.get("next_at") or 0) > now:
                        continue
                    try:
                        completed = await self.agent_runs.complete_run(
                            run_id,
                            result=dict(item.get("result") or {}),
                            message=str(item.get("message") or "Live Voice session terminated"),
                        )
                        if completed is not None or not self._agent_run_persistence_required:
                            async with self._lock:
                                self._pending_terminalizations.pop(run_id, None)
                            continue
                        raise RuntimeError("AgentRun completion returned no durable row")
                    except Exception as exc:
                        attempts = int(item.get("attempts") or 0) + 1
                        item["attempts"] = attempts
                        item["next_at"] = time.monotonic() + min(30.0, 0.1 * (2**min(attempts, 8)))
                        logger.debug(
                            "Live Voice AgentRun terminalization retry pending: %s",
                            type(exc).__name__,
                        )
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise

    async def _queue_tool_audit(
        self,
        session: LiveVoiceSession,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        success: bool,
    ) -> None:
        if not self._agent_run_persistence_required or not session.agent_run_id:
            return
        key = f"{session.agent_run_id}:{call_id}"
        async with self._lock:
            self._pending_tool_audits[key] = {
                "run_id": session.agent_run_id,
                "call_id": call_id,
                "tool_name": tool_name,
                "arguments": dict(arguments),
                "result": json.dumps(
                    _redact_json(dict(result)), ensure_ascii=False, default=str
                )[:MAX_TOOL_RESULT_CHARS],
                "success": bool(success),
                "next_at": time.monotonic() + 0.1,
                "attempts": 0,
            }
            task = self._tool_audit_retry_task
            if task is None or task.done():
                self._tool_audit_retry_task = asyncio.create_task(
                    self._retry_tool_audits(),
                    name="live-voice-agent-run-tool-audit-retry",
                )

    async def _queue_run_event(
        self,
        session: LiveVoiceSession,
        *,
        event_type: str,
        status: str | None,
        message: str | None,
        payload: Mapping[str, Any],
    ) -> None:
        if not self._agent_run_persistence_required or not session.agent_run_id:
            return
        event_id = str(payload.get("event_id") or "").strip()
        fingerprint = event_id or hashlib.sha256(
            json.dumps(
                _redact_json(dict(payload)),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        key = f"{session.agent_run_id}:{event_type}:{fingerprint}"
        async with self._lock:
            self._pending_run_events[key] = {
                "run_id": session.agent_run_id,
                "event_type": event_type,
                "status": status,
                "message": message,
                "payload": dict(_redact_json(dict(payload))),
                "attempts": 0,
                "next_at": time.monotonic() + 0.1,
            }
            while len(self._pending_run_events) > MAX_PENDING_AUDIT_OPERATIONS:
                self._pending_run_events.pop(next(iter(self._pending_run_events)))
            task = self._run_event_retry_task
            if task is None or task.done():
                self._run_event_retry_task = asyncio.create_task(
                    self._retry_run_events(),
                    name="live-voice-agent-run-event-retry",
                )

    async def _retry_run_events(self) -> None:
        try:
            while True:
                async with self._lock:
                    pending = list(self._pending_run_events.items())
                if not pending:
                    return
                now = time.monotonic()
                for key, item in pending:
                    if float(item.get("next_at") or 0) > now:
                        continue
                    try:
                        recorded = await self.agent_runs.record_event(
                            item.get("run_id"),
                            str(item.get("event_type") or "live_voice.event"),
                            status=item.get("status"),
                            message=item.get("message"),
                            payload=dict(item.get("payload") or {}),
                        )
                        if recorded is not None or not self._agent_run_persistence_required:
                            async with self._lock:
                                self._pending_run_events.pop(key, None)
                            continue
                        raise RuntimeError("AgentRun event persistence returned no durable row")
                    except Exception as exc:
                        attempts = int(item.get("attempts") or 0) + 1
                        item["attempts"] = attempts
                        item["next_at"] = time.monotonic() + min(30.0, 0.1 * (2**min(attempts, 8)))
                        logger.debug(
                            "Live Voice AgentRun event retry pending: %s", type(exc).__name__
                        )
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise

    async def _retry_tool_audits(self) -> None:
        try:
            while True:
                async with self._lock:
                    pending = list(self._pending_tool_audits.items())
                if not pending:
                    return
                now = time.monotonic()
                for key, item in pending:
                    if float(item.get("next_at") or 0) > now:
                        continue
                    try:
                        recorded = await self.agent_runs.record_tool_call(
                            item.get("run_id"),
                            tool_name=str(item.get("tool_name") or ""),
                            tool_call_id=str(item.get("call_id") or ""),
                            arguments=dict(item.get("arguments") or {}),
                            result=item.get("result"),
                            success=bool(item.get("success")),
                            mutation_confirmed=False,
                            metadata={
                                "source": "live_voice",
                                "event_source": EVENT_SOURCE_SIDEBAND,
                                "retry": True,
                            },
                        )
                        if recorded is not None or not self._agent_run_persistence_required:
                            async with self._lock:
                                self._pending_tool_audits.pop(key, None)
                            continue
                        raise RuntimeError("AgentRun tool audit returned no durable row")
                    except Exception as exc:
                        attempts = int(item.get("attempts") or 0) + 1
                        item["attempts"] = attempts
                        item["next_at"] = time.monotonic() + min(30.0, 0.1 * (2**min(attempts, 8)))
                        logger.debug(
                            "Live Voice AgentRun tool audit retry pending: %s",
                            type(exc).__name__,
                        )
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise

    async def _close_character_tts_output(self, session: LiveVoiceSession) -> None:
        """Close custom-TTS media resources for every terminal session path."""

        output = getattr(session, "_character_tts", None)
        if output is not None:
            close = getattr(output, "close", None)
            if callable(close):
                try:
                    await _maybe_await(close())
                except BaseException:
                    # Provider/session teardown is fail-closed even when a
                    # third-party TTS manager reports a close error.
                    logger.debug(
                        "Live Voice Character TTS close skipped", exc_info=True
                    )
        # Keep transport cleanup independent from the output wrapper.  A
        # partially initialized session (or a test/provider bootstrap failure)
        # can own the transport without ever constructing CharacterTTSOutput.
        transport = getattr(session, "audio_transport", None)
        if transport is not None:
            close = getattr(transport, "close", None)
            if callable(close):
                try:
                    await _maybe_await(close())
                except BaseException:
                    logger.debug(
                        "Live Voice Character TTS transport close skipped",
                        exc_info=True,
                    )

    async def _cancel_sideband_task(self, session: LiveVoiceSession) -> None:
        await self._stop_sideband_processor(session)
        task = self._sideband_tasks.pop(session.id, None)
        await self._cancel_task(task)
        # This is intentionally before the call-id early return: sessions that
        # never reached WebRTC still own a TTS worker/transport from start.
        await self._close_character_tts_output(session)
        call_id = str(session.call_id or "").strip()
        if not call_id:
            return
        # Sideband and provider call lifetimes are separate resources. Attempt
        # both independently: a failed hangup must never prevent local task,
        # media, registry, or audit cleanup.
        close_sideband = getattr(self.provider, "close_sideband", None)
        if close_sideband is not None:
            try:
                await _maybe_await(close_sideband(call_id))
            except Exception as exc:
                logger.debug("Live Voice sideband close skipped: %s", type(exc).__name__)
        hangup_call = getattr(self.provider, "hangup_call", None)
        if hangup_call is not None:
            try:
                await asyncio.wait_for(
                    _maybe_await(hangup_call(call_id)),
                    timeout=PROVIDER_HANGUP_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                logger.warning("Live Voice provider hangup skipped: %s", type(exc).__name__)

    async def _expire_sessions(self) -> None:
        # Snapshot without holding the service lock while taking a per-session
        # lifecycle lock.  Other paths use the same order (session lock, then
        # registry lock), avoiding lock inversion while connect/end race.
        async with self._lock:
            candidates = list(self._sessions.values())
        expired: list[LiveVoiceSession] = []
        for session in candidates:
            async with session._connect_lock:
                if not (
                    session.status == "failed"
                    or (session.status == "active" and self._session_expired(session))
                ):
                    continue
                if session.status == "active":
                    session.status = "expired"
                session._sideband_provenance = None
                session._connect_in_progress = False
                async with self._lock:
                    if self._sessions.get(session.id) is session:
                        self._sessions.pop(session.id, None)
                expired.append(session)
        for session in expired:
            if self._sideband_is_established(session):
                try:
                    await self._send_sideband_event(session, {"type": "response.cancel"})
                except Exception:
                    pass
            await self._cancel_sideband_task(session)
            session.call_id = None
            audit_pending = False
            if session.status == "expired":
                audit_pending = not await self._complete_agent_run(
                    session.agent_run_id,
                    result={"source": "live_voice", "live_session_id": session.id},
                    message="Live Voice session expired",
                )
            await self._broadcast(
                session,
                {
                    "event_type": "live_voice.expired",
                    "event_source": "server",
                    "agent_run_id": session.agent_run_id,
                    "audit_pending": audit_pending,
                },
            )

    async def _active_session(
        self, session_id: str, actor: LiveVoiceActor
    ) -> LiveVoiceSession:
        session = await self.get_session(session_id, actor)
        if session.status != "active":
            raise LiveVoiceError(
                "Live Voice session is no longer active", status_code=409
            )
        return session

    async def _reserve_actor_start(self, actor: LiveVoiceActor) -> None:
        now = time.monotonic()
        actor_id = actor.user_id
        async with self._lock:
            failures = [
                timestamp
                for timestamp in self._start_failures.get(actor_id, [])
                if now - timestamp < self.start_failure_window_seconds
            ]
            self._start_failures[actor_id] = failures
            active_count = sum(
                1
                for session in self._sessions.values()
                if session.status == "active" and session.actor.user_id == actor_id
            )
            if active_count + (1 if actor_id in self._starting_actors else 0) >= self.max_sessions_per_actor:
                raise LiveVoiceError(
                    "An active Live Voice session already exists for this actor",
                    status_code=409,
                )
            if len(failures) >= self.start_failure_limit:
                raise LiveVoiceError(
                    "Live Voice session start rate limit exceeded",
                    status_code=429,
                )
            self._starting_actors.add(actor_id)

    async def _release_actor_start(self, actor: LiveVoiceActor) -> None:
        async with self._lock:
            self._starting_actors.discard(actor.user_id)

    async def _record_start_failure(self, actor: LiveVoiceActor) -> None:
        now = time.monotonic()
        async with self._lock:
            failures = [
                timestamp
                for timestamp in self._start_failures.get(actor.user_id, [])
                if now - timestamp < self.start_failure_window_seconds
            ]
            failures.append(now)
            self._start_failures[actor.user_id] = failures

    async def _create_conversation_session(
        self,
        *,
        actor: LiveVoiceActor,
        character_name: str,
        project_id: str | None,
    ) -> tuple[str, str | None]:
        repo = self._repo()
        if repo is None:
            if self.db_manager is not None or self.repository_factory is not None:
                raise LiveVoiceProviderError(
                    "ConversationSession repository is unavailable", status_code=503
                )
            return str(uuid.uuid4()), project_id
        try:
            durable = await repo.create_session(
                user_id=actor.user_id,
                character_name=character_name[:200] or "assistant",
                title="",
                project_id=project_id,
            )
            session_id = str(durable.id)
            await repo.ensure_participant(
                session_id,
                "user",
                actor.user_id,
                display_name=actor.display_name,
                role="owner",
                status="joined",
                metadata={"source": "live_voice"},
            )
            return session_id, str(durable.project_id) if durable.project_id else project_id
        except Exception as exc:
            # Once a repository/DB is configured, silently falling back to an
            # in-memory UUID would bypass ConversationSession ACL and lose the
            # transcript.  Fail closed instead; only a deliberately credential-
            # free service (no repository at all) uses an ephemeral session.
            logger.warning("Live Voice ConversationSession persistence failed: %s", type(exc).__name__)
            raise LiveVoiceProviderError(
                "ConversationSession persistence is unavailable", status_code=503
            ) from exc

    async def _load_conversation_session(self, conversation_session_id: str) -> Any | None:
        repo = self._repo()
        if repo is None:
            if self.db_manager is not None or self.repository_factory is not None:
                raise LiveVoiceProviderError(
                    "ConversationSession repository is unavailable", status_code=503
                )
            return None
        try:
            return await repo.get_session_by_id(conversation_session_id, with_messages=False)
        except Exception as exc:
            logger.warning("Live Voice ConversationSession lookup failed: %s", type(exc).__name__)
            raise LiveVoiceProviderError(
                "ConversationSession lookup is unavailable", status_code=503
            ) from exc

    async def _create_agent_run(
        self,
        *,
        actor: LiveVoiceActor,
        session: LiveVoiceSession,
        objective: str,
    ) -> str:
        client_message_id = f"live-voice:{session.id}"
        run_id = ""
        try:
            result = await self.agent_runs.create_run(
                session_id=session.conversation_session_id,
                user_id=actor.user_id,
                client_message_id=client_message_id,
                objective=objective[:MAX_TRANSCRIPT_CHARS],
                run_type="live_voice_session",
                metadata={
                    "source": "live_voice",
                    "live_session_id": session.id,
                    "actor_id": actor.user_id,
                },
                title="Live Voice",
                provider=session.provider,
                model=session.model,
            )
            run_id = str(result.get("id") or "").strip() if isinstance(result, Mapping) else ""
            if run_id:
                marked = await self.agent_runs.mark_running(
                    run_id,
                    message="Live Voice session started",
                    provider=session.provider,
                    model=session.model,
                    metadata={"source": "live_voice", "live_session_id": session.id},
                )
                if self._agent_run_persistence_required and marked is None:
                    # The run row exists, but failed to transition out of
                    # queued. Compensate with a terminal update (or durable
                    # retry intent) before aborting provider initialization.
                    raise LiveVoiceProviderError(
                        "AgentRun persistence is unavailable", status_code=503
                    )
                return run_id
        except Exception as exc:
            logger.warning("Live Voice AgentRun persistence skipped: %s", type(exc).__name__)
            if self._agent_run_persistence_required:
                if run_id:
                    await self._complete_agent_run(
                        run_id,
                        result={"source": "live_voice", "status": "failed"},
                        message="Live Voice AgentRun start failed",
                    )
                raise LiveVoiceProviderError(
                    "AgentRun persistence is unavailable", status_code=503
                ) from exc
        if self._agent_run_persistence_required:
            raise LiveVoiceProviderError(
                "AgentRun persistence returned no durable run", status_code=503
            )
        # The runtime identity is still useful for browser progress events when
        # the optional database is unavailable. It is never presented as a
        # durable row unless the existing AgentRunService created one.
        return str(uuid.uuid4())

    async def start_session(
        self,
        *,
        actor: LiveVoiceActor,
        conversation_session_id: str | None = None,
        character_name: str = "assistant",
        project_id: str | None = None,
        include_project_context: bool | None = None,
        model: str | None = None,
        voice: str | None = None,
        instructions: str | None = None,
    ) -> dict[str, Any]:
        return await self.voice_session_service().start_legacy_session(
            actor=actor,
            conversation_session_id=conversation_session_id,
            character_name=character_name,
            project_id=project_id,
            include_project_context=include_project_context,
            model=model,
            voice=voice,
            instructions=instructions,
        )

    async def _start_session_unreserved(
        self,
        *,
        actor: LiveVoiceActor,
        conversation_session_id: str | None = None,
        character_name: str = "assistant",
        project_id: str | None = None,
        include_project_context: bool | None = None,
        model: str | None = None,
        voice: str | None = None,
        instructions: str | None = None,
        policy: Any | None = None,
        mode: Any | None = None,
    ) -> dict[str, Any]:
        normalized_model = str(model or os.getenv("OPENAI_REALTIME_MODEL") or DEFAULT_REALTIME_MODEL).strip()
        normalized_voice = str(voice or os.getenv("OPENAI_REALTIME_VOICE") or DEFAULT_REALTIME_VOICE).strip()
        if (
            not normalized_model
            or len(normalized_model) > 160
            or normalized_model not in self._allowed_models
        ):
            raise LiveVoiceError("Unsupported Realtime model", status_code=400)
        if (
            not normalized_voice
            or len(normalized_voice) > 80
            or normalized_voice not in self._allowed_voices
        ):
            raise LiveVoiceError("Unsupported Realtime voice", status_code=400)
        # The unified call is created only after the browser has produced its
        # SDP offer, but a missing server credential can be rejected now without
        # touching the network or allocating a durable ConversationSession.
        ready_check = getattr(self.provider, "check_ready", None)
        if ready_check is not None:
            try:
                await _maybe_await(ready_check())
            except LiveVoiceError:
                raise
            except Exception as exc:
                raise LiveVoiceProviderError(
                    "Live Voice provider is not ready", status_code=503
                ) from exc
        normalized_instructions = str(instructions or "").replace("\x00", "").strip()
        if len(normalized_instructions) > MAX_TRANSCRIPT_CHARS:
            normalized_instructions = normalized_instructions[:MAX_TRANSCRIPT_CHARS]
        from .voice_sessions.models import VoiceSessionMode
        from .voice_sessions.policy import VoiceSessionPolicyResolver

        resolved_policy = policy
        if resolved_policy is None:
            resolved_policy = VoiceSessionPolicyResolver.resolve(
                config=self.config,
                actor=actor,
                requested_mode=mode,
                character_name=character_name,
                allow_legacy_overrides=True,
                legacy_model=normalized_model,
                legacy_voice=normalized_voice,
                legacy_instructions=normalized_instructions or None,
            )
        resolved_mode = str(mode or resolved_policy.mode)
        if conversation_session_id:
            conversation_session_id = str(conversation_session_id).strip()
            if not conversation_session_id:
                conversation_session_id = None
        durable = None
        if conversation_session_id is None:
            conversation_session_id, project_id = await self._create_conversation_session(
                actor=actor,
                character_name=character_name,
                project_id=project_id,
            )
        else:
            durable = await self._load_conversation_session(conversation_session_id)
            if durable is None:
                raise LiveVoiceNotFoundError("ConversationSession not found")
            if durable is not None:
                owner = str(getattr(durable, "user_id", ""))
                participants = getattr(durable, "participants", []) or []
                visible = owner == actor.user_id or any(
                    str(getattr(item, "participant_id", "")) == actor.user_id
                    and str(getattr(item, "status", "joined")) == "joined"
                    for item in participants
                    if str(getattr(item, "participant_type", "")) == "user"
                )
                if actor.role != "admin" and not visible:
                    raise LiveVoicePermissionError("ConversationSession access denied")
                project_id = str(getattr(durable, "project_id", None) or project_id or "") or None

        # local_only must fail before AgentRun allocation and before a provider
        # can open an external transport. Resolve persisted session/project
        # metadata first so browser reconnects cannot bypass an override.
        session_context, project_metadata = await self._resolve_privacy_scope(
            project_id=project_id,
            durable_session=durable,
        )
        privacy_mode = self._privacy_preflight(
            actor,
            session_id=conversation_session_id,
            project_id=project_id,
            session_context=session_context,
            project_metadata=project_metadata,
        )

        live_session = LiveVoiceSession(
            id=str(uuid.uuid4()),
            actor=actor,
            conversation_session_id=conversation_session_id,
            provider=str(getattr(self.provider, "name", LIVE_VOICE_PROVIDER) or LIVE_VOICE_PROVIDER),
            model=normalized_model,
            voice=normalized_voice,
            project_id=project_id,
            character_name=character_name[:200] or "assistant",
            include_project_context=(
                bool(include_project_context)
                if include_project_context is not None
                else None
            ),
            session_context=session_context,
            project_metadata=project_metadata,
            privacy_mode=privacy_mode,
            _instructions=normalized_instructions or None,
            mode=resolved_mode,
            policy=resolved_policy,
        )
        if resolved_mode == VoiceSessionMode.REALTIME_CHARACTER_TTS.value:
            from .voice_sessions.audio_transport import VoiceAudioTransportManager

            transport = VoiceAudioTransportManager(
                unacked_window=1,
                queue_maxsize=2,
            )
            live_session.audio_transport = transport
            live_session._character_tts = self._build_character_tts_output(
                session=live_session,
                policy=resolved_policy,
                transport=transport,
            )
        live_session.turn_context = TurnContext(
            user_id=actor.user_id,
            project_id=project_id,
            session_id=conversation_session_id,
            client_message_id=f"live-voice:{live_session.id}",
            include_project_context=live_session.include_project_context,
        )
        turn_token = set_turn_context(
            user_id=actor.user_id,
            project_id=project_id,
            session_id=conversation_session_id,
            client_message_id=f"live-voice:{live_session.id}",
            include_project_context=live_session.include_project_context,
        )
        try:
            # Establish the durable AgentRun before registering the runtime;
            # a configured audit/database outage must not leave a
            # browser-visible session without a durable run.
            live_session.agent_run_id = await self._create_agent_run(
                actor=actor,
                session=live_session,
                objective=normalized_instructions or "Live Voice conversation",
            )
        except asyncio.CancelledError:
            # Request disconnects must not strand a durable AgentRun or actor
            # start reservation even though CancelledError is a BaseException.
            if live_session.agent_run_id:
                try:
                    await asyncio.shield(
                        self._complete_agent_run(
                            live_session.agent_run_id,
                            result={"source": "live_voice", "status": "cancelled"},
                            message="Live Voice session initialization cancelled",
                        )
                    )
                except BaseException:
                    pass
            raise
        except LiveVoiceError:
            if live_session.agent_run_id:
                await self._complete_agent_run(
                    live_session.agent_run_id,
                    result={"source": "live_voice", "status": "failed"},
                    message="Live Voice session initialization failed",
                )
            raise
        except Exception as exc:
            logger.warning("Live Voice provider initialization failed: %s", type(exc).__name__)
            if live_session.agent_run_id:
                await self._complete_agent_run(
                    live_session.agent_run_id,
                    result={"source": "live_voice", "status": "failed"},
                    message="Live Voice session initialization failed",
                )
            raise LiveVoiceProviderError("Live Voice provider initialization failed", status_code=502) from exc
        finally:
            reset_turn_context(turn_token)
        async with self._lock:
            self._sessions[live_session.id] = live_session
        await self._ensure_cleanup_task()
        await self._broadcast(
            live_session,
            {"type": "session.created", "session": live_session.to_dict()},
        )
        return {
            "session": live_session.to_dict(),
            "provider": live_session.provider,
            "model": live_session.model,
        }

    async def get_session(self, session_id: str, actor: LiveVoiceActor) -> LiveVoiceSession:
        normalized = str(session_id or "").strip()
        await self._expire_sessions()
        async with self._lock:
            session = self._sessions.get(normalized)
        if session is None:
            raise LiveVoiceNotFoundError()
        if actor.role != "admin" and session.actor.user_id != actor.user_id:
            raise LiveVoicePermissionError()
        return session

    async def list_sessions(self, actor: LiveVoiceActor) -> list[dict[str, Any]]:
        """List in-process Live Voice sessions visible to the actor."""

        await self._expire_sessions()
        async with self._lock:
            sessions = list(self._sessions.values())
        if actor.role != "admin":
            sessions = [item for item in sessions if item.actor.user_id == actor.user_id]
        sessions.sort(key=lambda item: item.last_activity_at, reverse=True)
        return [item.to_dict() for item in sessions]

    async def _broadcast(self, session: LiveVoiceSession, data: Mapping[str, Any]) -> None:
        if self.broadcaster is None:
            return
        payload = {
            "type": "live_voice.event",
            "session_id": session.conversation_session_id,
            "live_session_id": session.id,
            "data": _redact_json(dict(data)),
        }
        try:
            await _maybe_await(
                self.broadcaster(
                    payload,
                    session_id=session.conversation_session_id,
                    user_id=session.actor.user_id,
                )
            )
        except TypeError:
            await _maybe_await(self.broadcaster(payload))
        except Exception as exc:
            logger.warning("Live Voice progress broadcast failed: %s", type(exc).__name__)

    async def _persist_transcript(
        self,
        *,
        session: LiveVoiceSession,
        role: str,
        transcript: str,
        event: Mapping[str, Any],
        event_source: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        # Browser telemetry is never authoritative for ConversationMessage
        # rows. Only the provider-owned sideband can persist a transcript.
        if event_source != EVENT_SOURCE_SIDEBAND:
            return None
        repo = self._repo()
        if repo is None:
            if self.db_manager is not None or self.repository_factory is not None:
                raise LiveVoiceProviderError(
                    "ConversationMessage repository is unavailable", status_code=503
                )
            return None
        try:
            message = await repo.add_message(
                session.conversation_session_id,
                role,
                transcript,
                metadata={
                    "source": "live_voice",
                    "provider": session.provider,
                    "event_type": _event_type(event),
                    "event_id": _event_id(event),
                    "event_source": event_source,
                    "live_session_id": session.id,
                    **(dict(metadata) if metadata else {}),
                },
                sender_type="user" if role == "user" else "character",
                sender_id=session.actor.user_id if role == "user" else session.provider,
                sender_display_name=session.actor.display_name if role == "user" else "Live Voice",
            )
            message_id = str(getattr(message, "id", "") or "").strip() or None
            if message_id:
                session._transcript_message_ids.append(message_id)
            return message_id
        except Exception as exc:
            logger.warning("Live Voice transcript persistence failed: %s", type(exc).__name__)
            if self.db_manager is not None or self.repository_factory is not None:
                raise LiveVoiceProviderError(
                    "ConversationMessage persistence is unavailable", status_code=503
                ) from exc
            return None

    async def handle_event(
        self,
        session_id: str,
        actor: LiveVoiceActor,
        event: Mapping[str, Any],
        *,
        source: str = EVENT_SOURCE_BROWSER,
        provenance: object | None = None,
    ) -> dict[str, Any]:
        session = await self._active_session(session_id, actor)
        if not isinstance(event, Mapping):
            raise LiveVoiceError("Realtime event must be an object", status_code=400)
        event = dict(event)
        normalized_source = str(source or "").strip()
        if normalized_source == "sideband":
            normalized_source = EVENT_SOURCE_SIDEBAND
        if normalized_source not in {EVENT_SOURCE_BROWSER, EVENT_SOURCE_SIDEBAND}:
            raise LiveVoicePermissionError("Unknown Live Voice event provenance")
        if normalized_source == EVENT_SOURCE_SIDEBAND:
            # The token is generated when this server opens the provider
            # sideband. A request body cannot forge it by adding ``source``.
            if (
                provenance is None
                or session._sideband_provenance is None
                or provenance is not session._sideband_provenance
            ):
                raise LiveVoicePermissionError("Provider sideband provenance is required")
            restorer = getattr(self.provider, "restore_sideband_event", None)
            if callable(restorer) and session.call_id:
                try:
                    restored = await _maybe_await(
                        restorer(call_id=session.call_id, event=event)
                    )
                    if isinstance(restored, Mapping):
                        event = dict(restored)
                except Exception:
                    logger.debug(
                        "Live Voice transcript alias restore skipped", exc_info=True
                    )
        event_type = _event_type(event)
        if not event_type:
            raise LiveVoiceError("Realtime event type is required", status_code=400)
        if (
            normalized_source == EVENT_SOURCE_BROWSER
            and event_type not in BROWSER_TELEMETRY_EVENT_TYPES
        ):
            # Browser POST /events is intentionally telemetry-only. Reject
            # transcript, provider, function-call, and tool-shaped payloads
            # before dedupe, AgentRun, broadcast, or persistence.
            raise LiveVoicePermissionError(
                "Browser events may report lifecycle telemetry only"
            )
        incoming_id = _event_id(event)
        dedupe_key = _event_key(event)
        seen = (
            session._seen_sideband_event_keys
            if normalized_source == EVENT_SOURCE_SIDEBAND
            else session._seen_browser_event_ids
        )
        if dedupe_key in seen:
            return {
                "accepted": True,
                "duplicate": True,
                "event_id": incoming_id,
                "event_source": normalized_source,
                "session": session.to_dict(),
            }
        if normalized_source == EVENT_SOURCE_BROWSER:
            if session._browser_event_count >= MAX_BROWSER_TELEMETRY_EVENTS:
                raise LiveVoiceError(
                    "Live Voice telemetry rate limit exceeded", status_code=429
                )
            session._browser_event_count += 1
        seen.add(dedupe_key)
        if normalized_source == EVENT_SOURCE_BROWSER:
            if len(seen) > MAX_SIDEBAND_EVENT_IDS:
                session._seen_browser_event_ids = set(
                    list(session._seen_browser_event_ids)[-MAX_SIDEBAND_EVENT_IDS // 2 :]
                )
            session.event_count += 1
            session.last_event_type = event_type
            # Do not persist/broadcast browser telemetry. This result is safe
            # for the caller and explicitly labels the untrusted source.
            return {
                "accepted": True,
                "event_type": event_type,
                "event_id": incoming_id,
                "event_source": EVENT_SOURCE_BROWSER,
                "session": session.to_dict(),
            }
        if len(seen) > MAX_SIDEBAND_EVENT_IDS:
            session._seen_sideband_event_keys = set(
                list(session._seen_sideband_event_keys)[-MAX_SIDEBAND_EVENT_IDS // 2 :]
            )
        session._seen_event_ids.add(incoming_id or dedupe_key)
        if len(session._seen_event_ids) > MAX_SIDEBAND_EVENT_IDS:
            session._seen_event_ids = set(
                list(session._seen_event_ids)[-MAX_SIDEBAND_EVENT_IDS // 2 :]
            )
        session.event_count += 1
        session.last_event_type = event_type
        session.last_activity_at = datetime.now(timezone.utc)
        turn_token = set_turn_context(
            user_id=session.actor.user_id,
            project_id=session.project_id,
            session_id=session.conversation_session_id,
            client_message_id=f"live-voice:{session.id}",
            include_project_context=session.include_project_context,
        )
        try:
            from .voice_sessions.models import VoiceSessionMode

            if (
                event_type == "input_audio_buffer.speech_started"
                and session.mode != VoiceSessionMode.REALTIME_CHARACTER_TTS.value
            ):
                policy = session.policy
                if policy is not None and policy.interrupt_response:
                    if self._sideband_is_established(session):
                        await self._send_sideband_event(session, {"type": "response.cancel"})
                return {
                    "accepted": True,
                    "event_type": event_type,
                    "event_id": incoming_id,
                    "event_source": EVENT_SOURCE_SIDEBAND,
                    "session": session.to_dict(),
                }

            if session.mode == VoiceSessionMode.REALTIME_CHARACTER_TTS.value:
                handled = await self._handle_character_tts_sideband_event(
                    session, event_type, event
                )
                if handled is not None:
                    return handled
            transcript_role: str | None = None
            if event_type in {
                "conversation.item.input_audio_transcription.completed",
                "conversation.item.input_audio_transcription.done",
            }:
                transcript_role = "user"
            elif event_type in {
                "response.audio_transcript.done",
                "response.output_audio_transcript.done",
                "response.output_text.done",
            }:
                transcript_role = "assistant"
            transcript = _extract_transcript(event)
            result: dict[str, Any] = {
                "accepted": True,
                "event_type": event_type,
                "event_id": incoming_id,
                "event_source": EVENT_SOURCE_SIDEBAND,
                "session": session.to_dict(),
            }
            if transcript_role and transcript:
                # Transcript durability is serialized with close/expiry. If
                # this block acquires the lock first, /end waits until the
                # database commit and authoritative broadcast complete; if a
                # terminal transition wins first, no transcript is persisted.
                async with session._connect_lock:
                    async with self._lock:
                        registered = self._sessions.get(session.id) is session
                    if (
                        session.status != "active"
                        or not registered
                        or session._sideband_provenance is not provenance
                    ):
                        raise LiveVoiceError(
                            "Live Voice session is no longer active", status_code=409
                        )
                    message_id = await self._persist_transcript(
                        session=session,
                        role=transcript_role,
                        transcript=transcript,
                        event=event,
                        event_source=EVENT_SOURCE_SIDEBAND,
                    )
                    await self._record_run_event(
                        session,
                        "live_voice.transcript",
                        status="succeeded",
                        message=transcript,
                        payload={
                            "source": "live_voice",
                            "event_source": EVENT_SOURCE_SIDEBAND,
                            "event_type": event_type,
                            "event_id": incoming_id,
                            "role": transcript_role,
                            "transcript": transcript,
                            "message_id": message_id,
                        },
                    )
                    result.update(
                        {"role": transcript_role, "transcript": transcript, "message_id": message_id}
                    )
                    await self._broadcast(
                        session,
                        {
                            "event_type": event_type,
                            "event_id": incoming_id,
                            "event_source": EVENT_SOURCE_SIDEBAND,
                            "role": transcript_role,
                            "transcript": transcript,
                            "message_id": message_id,
                            "agent_run_id": session.agent_run_id,
                        },
                    )
            function_call = _function_call_from_event(event)
            if function_call is not None:
                call_id, tool_name, arguments = function_call
                tool_result = await self.process_tool_call(
                    session,
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    provenance=provenance,
                )
                result["tool_call"] = tool_result
            if event_type == "response.done":
                usage = event.get("usage")
                await self._record_run_event(
                    session,
                    "live_voice.response.done",
                    status="running",
                    message="Realtime response completed",
                    payload={
                        "source": "live_voice",
                        "event_source": EVENT_SOURCE_SIDEBAND,
                        "event_type": event_type,
                        "event_id": incoming_id,
                        "usage": _redact_json(usage) if isinstance(usage, Mapping) else None,
                    },
                )
            if event_type == "error":
                # Provider error events are terminal.  Do not merely mark the
                # in-memory object failed: clear the ephemeral secret/call,
                # remove it from the registry, close sideband resources and
                # notify the browser just like a transport disconnect.
                await self._mark_sideband_failed(session)
            result["session"] = session.to_dict()
            return result
        except LiveVoiceProviderError:
            # Durable transcript/AgentRun/tool-audit failures are critical for
            # a provider-authoritative event.  Fail the live call closed rather
            # than leaving an active session that can continue without an
            # auditable history.  The original 503 is returned to the caller.
            if normalized_source == EVENT_SOURCE_SIDEBAND:
                await self._mark_sideband_failed(session)
            raise
        finally:
            reset_turn_context(turn_token)

    async def _record_run_event(
        self,
        session: LiveVoiceSession,
        event_type: str,
        *,
        status: str | None,
        message: str | None,
        payload: Mapping[str, Any],
    ) -> None:
        try:
            recorded = await self.agent_runs.record_event(
                session.agent_run_id,
                event_type,
                status=status,
                message=message,
                payload=dict(payload),
            )
            if self._agent_run_persistence_required and recorded is None:
                raise RuntimeError("AgentRun event persistence returned no durable row")
        except Exception as exc:
            logger.warning("Live Voice AgentRun event persistence skipped: %s", type(exc).__name__)
            if self._agent_run_persistence_required:
                await self._queue_run_event(
                    session,
                    event_type=event_type,
                    status=status,
                    message=message,
                    payload=payload,
                )
                raise LiveVoiceProviderError(
                    "AgentRun event persistence is unavailable", status_code=503
                ) from exc

    async def process_tool_call(
        self,
        session: LiveVoiceSession,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        provenance: object | None = None,
    ) -> dict[str, Any]:
        """Execute one provider function call at most once per call_id."""

        normalized_call_id = str(call_id or "").strip()
        if not normalized_call_id:
            return {
                "call_id": "",
                "tool": str(tool_name or "").strip(),
                "approved": False,
                "success": False,
                "error": "tool call id is required",
            }
        async with session._tool_call_lock:
            cached = session._tool_call_results.get(normalized_call_id)
            if cached is not None:
                return {**cached, "duplicate": True}
            pending = session._tool_call_inflight.get(normalized_call_id)
            if pending is None:
                pending = asyncio.get_running_loop().create_future()
                pending.add_done_callback(
                    lambda future: future.exception()
                    if not future.cancelled()
                    else None
                )
                session._tool_call_inflight[normalized_call_id] = pending
                owner = True
            else:
                owner = False
        if not owner:
            try:
                result = await asyncio.shield(pending)
            except asyncio.CancelledError:
                raise
            return {**result, "duplicate": True}
        try:
            result = await self._process_tool_call_once(
                session,
                call_id=normalized_call_id,
                tool_name=tool_name,
                arguments=arguments,
                provenance=provenance,
            )
        except BaseException as exc:
            async with session._tool_call_lock:
                session._tool_call_inflight.pop(normalized_call_id, None)
                if not pending.done():
                    pending.set_exception(exc)
            raise
        async with session._tool_call_lock:
            session._tool_call_inflight.pop(normalized_call_id, None)
            session._tool_call_results[normalized_call_id] = dict(result)
            while len(session._tool_call_results) > MAX_COMPLETED_TOOL_CALLS:
                session._tool_call_results.pop(next(iter(session._tool_call_results)))
            if not pending.done():
                pending.set_result(dict(result))
        return result

    async def _process_tool_call_once(
        self,
        session: LiveVoiceSession,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        provenance: object | None = None,
    ) -> dict[str, Any]:
        normalized_tool = str(tool_name or "").strip()
        if not normalized_tool:
            return {"call_id": call_id, "approved": False, "error": "tool name is required"}
        normalized_call_id = str(call_id or "").strip()
        if not normalized_call_id:
            return {
                "call_id": "",
                "tool": normalized_tool,
                "approved": False,
                "success": False,
                "error": "tool call id is required",
            }
        call_id = normalized_call_id
        async with session._connect_lock:
            async with self._lock:
                registered = self._sessions.get(session.id) is session
            session_live = (
                session.status == "active"
                and registered
                and session._sideband_provenance is provenance
            )
        if not session_live:
            result = {
                "call_id": call_id,
                "tool": normalized_tool,
                "approved": False,
                "success": False,
                "error": "Live Voice session is no longer active",
            }
            await self._send_tool_response(
                session, call_id=call_id, safe_error="Live Voice session is no longer active"
            )
            return result
        if (
            provenance is None
            or session._sideband_provenance is None
            or provenance is not session._sideband_provenance
        ):
            # Even an authenticated in-process caller cannot turn an arbitrary
            # request into a tool invocation; only the provider sideband token
            # may reach this boundary.
            result = {
                "call_id": call_id,
                "tool": normalized_tool,
                "approved": False,
                "success": False,
                "error": "Provider sideband provenance is required",
            }
            await self._record_tool_call(
                session,
                call_id,
                normalized_tool,
                {},
                result,
                False,
            )
            await self._send_tool_response(
                session, call_id=call_id, safe_error="Tool call denied"
            )
            return result
        safe_arguments = dict(_redact_json(dict(arguments)))
        if normalized_tool not in self._allowed_tools:
            result = {
                "call_id": call_id,
                "tool": normalized_tool,
                "approved": False,
                "success": False,
                "error": "Tool is not allowlisted",
            }
            await self._record_run_event(
                session,
                "live_voice.tool.permission_denied",
                status="failed",
                message=normalized_tool,
                payload={
                    "source": "live_voice",
                    "event_source": EVENT_SOURCE_SIDEBAND,
                    "tool": normalized_tool,
                    "tool_call_id": call_id,
                    "approved": False,
                    "reason": "allowlist",
                },
            )
            await self._record_tool_call(
                session,
                call_id,
                normalized_tool,
                safe_arguments,
                result,
                False,
            )
            await self._send_tool_response(
                session, call_id=call_id, safe_error="Tool is not available"
            )
            return result
        approved = False
        permission_scope_token = None
        try:
            from ..tools.external_llm_permission import set_permission_session_key

            permission_scope_token = set_permission_session_key(
                f"{session.actor.user_id}|{session.conversation_session_id}"
            )
            if self.permission_checker is not None:
                approved = bool(
                    await _maybe_await(
                        self.permission_checker(normalized_tool, safe_arguments, session=session)
                    )
                )
            else:
                from ..tools.external_llm_permission import check_permission

                approved = bool(await check_permission(normalized_tool, safe_arguments))
        except TypeError:
            # Simple two-argument test adapters remain supported.
            if self.permission_checker is not None:
                approved = bool(
                    await _maybe_await(self.permission_checker(normalized_tool, safe_arguments))
                )
        except Exception:
            approved = False
        finally:
            if permission_scope_token is not None:
                try:
                    from ..tools.external_llm_permission import reset_permission_session_key

                    reset_permission_session_key(permission_scope_token)
                except Exception:
                    pass
        permission_event = "live_voice.tool.permission_granted" if approved else "live_voice.tool.permission_denied"
        await self._record_run_event(
            session,
            permission_event,
            status="succeeded" if approved else "failed",
            message=normalized_tool,
            payload={
                "source": "live_voice",
                "event_source": EVENT_SOURCE_SIDEBAND,
                "tool": normalized_tool,
                "tool_call_id": call_id,
                "arguments": safe_arguments,
                "approved": approved,
            },
        )
        if not approved:
            result = {
                "call_id": call_id,
                "tool": normalized_tool,
                "approved": False,
                "success": False,
                "error": "Tool permission denied",
            }
            await self._record_tool_call(session, call_id, normalized_tool, safe_arguments, result, False)
            await self._send_tool_response(
                session, call_id=call_id, safe_error="Tool permission denied"
            )
            return result
        # Durable intent is written before invoking any mutating tool. A
        # configured AgentRun persistence outage raises here, so the executor
        # is never called without an auditable request.
        await self._record_run_event(
            session,
            "live_voice.tool.requested",
            status="running",
            message=normalized_tool,
            payload={
                "source": "live_voice",
                "event_source": EVENT_SOURCE_SIDEBAND,
                "tool": normalized_tool,
                "tool_call_id": call_id,
                "arguments": safe_arguments,
                "approved": True,
            },
        )
        # Hold the lifecycle lock through the final status/provenance check and
        # executor await. If /end or TTL wins the lock while permission is
        # pending, the executor is never invoked; if the executor wins first,
        # close waits for its in-flight mutation to finish.
        async with session._connect_lock:
            async with self._lock:
                registered = self._sessions.get(session.id) is session
            lifecycle_valid = (
                session.status == "active"
                and registered
                and session._sideband_provenance is provenance
            )
            if not lifecycle_valid:
                result = {
                    "call_id": call_id,
                    "tool": normalized_tool,
                    "approved": False,
                    "success": False,
                    "error": "Live Voice session is no longer active",
                }
            else:
                try:
                    executor = self.tool_executor
                    if executor is None:
                        # Reuse AoiTalk's canonical registry rather than
                        # creating a Live Voice-specific tool set. The
                        # registry's tool functions observe the surrounding
                        # TurnContext for actor/project ACLs.
                        from ..tools.registry import get_registry

                        registry = get_registry()
                        resolved_name = normalized_tool
                        definition = self._canonical_voice_tool_map().get(resolved_name)
                        if definition is None and resolved_name not in registry:
                            resolved_name = normalized_tool.rsplit(".", 1)[-1]
                            definition = self._canonical_voice_tool_map().get(resolved_name)
                        if definition is None and resolved_name in registry:
                            definition = registry.get(resolved_name)
                        if definition is None:
                            raise ValueError(f"Tool not found: {normalized_tool}")
                        output = await definition.execute_async(**safe_arguments)
                    else:
                        try:
                            output = await _maybe_await(
                                executor(normalized_tool, safe_arguments, session=session)
                            )
                        except TypeError:
                            output = await _maybe_await(executor(normalized_tool, safe_arguments))
                    result = {
                        "call_id": call_id,
                        "tool": normalized_tool,
                        "approved": True,
                        "success": True,
                        "output": _redact_json(output, max_chars=MAX_TOOL_RESULT_CHARS),
                    }
                except Exception as exc:
                    result = {
                        "call_id": call_id,
                        "tool": normalized_tool,
                        "approved": True,
                        "success": False,
                        "error": str(exc)[:MAX_TOOL_RESULT_CHARS],
                    }
        await self._record_tool_call(
            session,
            call_id,
            normalized_tool,
            safe_arguments,
            result,
            bool(result.get("success")),
        )
        if result.get("success"):
            await self._send_tool_response(
                session, call_id=call_id, output=result.get("output")
            )
        else:
            await self._send_tool_response(
                session, call_id=call_id, safe_error="Tool execution failed"
            )
        await self._broadcast(
            session,
            {
                "event_type": permission_event,
                "event_source": EVENT_SOURCE_SIDEBAND,
                "tool_call": result,
            },
        )
        return result

    async def _record_tool_call(
        self,
        session: LiveVoiceSession,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        success: bool,
    ) -> None:
        try:
            recorded = await self.agent_runs.record_tool_call(
                session.agent_run_id,
                tool_name=tool_name,
                tool_call_id=call_id,
                arguments=dict(arguments),
                result=json.dumps(_redact_json(dict(result)), ensure_ascii=False, default=str)[:MAX_TOOL_RESULT_CHARS],
                success=success,
                mutation_confirmed=False,
                metadata={
                    "source": "live_voice",
                    "event_source": EVENT_SOURCE_SIDEBAND,
                    "live_session_id": session.id,
                },
            )
            if self._agent_run_persistence_required and recorded is None:
                raise RuntimeError("AgentRun tool audit returned no durable row")
        except Exception as exc:
            logger.warning("Live Voice tool audit persistence skipped: %s", type(exc).__name__)
            if self._agent_run_persistence_required:
                await self._queue_tool_audit(
                    session,
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    result=result,
                    success=success,
                )
                raise LiveVoiceProviderError(
                    "AgentRun tool audit persistence is unavailable", status_code=503
                ) from exc

    async def _send_sideband_event(
        self, session: LiveVoiceSession, event: Mapping[str, Any]
    ) -> None:
        sender = getattr(self.provider, "send_sideband_event", None)
        if sender is None or not session.call_id:
            raise LiveVoiceProviderError("Realtime sideband is unavailable", status_code=502)
        try:
            await asyncio.wait_for(
                _maybe_await(sender(call_id=session.call_id, event=dict(event))),
                timeout=SIDEBAND_SETUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise LiveVoiceProviderError(
                "Realtime sideband setup timed out", status_code=502
            ) from exc

    def _sideband_is_established(self, session: LiveVoiceSession) -> bool:
        if not session.call_id:
            return False
        checker = getattr(self.provider, "has_sideband", None)
        if checker is None:
            return True
        try:
            return bool(checker(session.call_id))
        except Exception:
            return False

    def _register_sideband_confirmation(
        self,
        session: LiveVoiceSession,
        *,
        expect_type: str,
        matcher: Callable[[Mapping[str, Any]], bool],
    ) -> asyncio.Future[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        session._sideband_confirmations.append((future, expect_type, matcher))
        return future

    def _dispatch_sideband_confirmations(
        self,
        session: LiveVoiceSession,
        event: Mapping[str, Any],
    ) -> None:
        event_type = _event_type(event) or ""
        pending = session._sideband_confirmations
        if not pending:
            return
        if event_type == "error":
            error_payload = event.get("error")
            if isinstance(error_payload, Mapping):
                message = str(error_payload.get("message") or "Realtime sideband error")
            else:
                message = str(event.get("message") or "Realtime sideband error")
            error = LiveVoiceProviderError(message, status_code=502)
            for future, _, _ in list(pending):
                if not future.done():
                    future.set_exception(error)
            pending.clear()
            return
        remaining: list[tuple[asyncio.Future[dict[str, Any]], str, Callable[[Mapping[str, Any]], bool]]] = []
        for future, expect_type, matcher in pending:
            if future.done():
                continue
            if event_type != expect_type:
                remaining.append((future, expect_type, matcher))
                continue
            try:
                if matcher(event):
                    future.set_result(dict(event))
                    continue
            except Exception:
                future.set_exception(
                    LiveVoiceProviderError(
                        "Sideband confirmation matcher failed",
                        status_code=502,
                    )
                )
                continue
            remaining.append((future, expect_type, matcher))
        session._sideband_confirmations[:] = remaining

    async def _wait_for_sideband_confirmation(
        self,
        session: LiveVoiceSession,
        *,
        expect_type: str,
        matcher: Callable[[Mapping[str, Any]], bool],
        timeout: float = SIDEBAND_SETUP_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        future = self._register_sideband_confirmation(
            session,
            expect_type=expect_type,
            matcher=matcher,
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            if not future.done():
                future.cancel()
            raise LiveVoiceProviderError(
                f"Realtime sideband confirmation timed out ({expect_type})",
                status_code=502,
            ) from exc
        finally:
            session._sideband_confirmations[:] = [
                item
                for item in session._sideband_confirmations
                if item[0] is not future
            ]

    async def _emit_mock_sideband_confirmation_if_available(
        self,
        session: LiveVoiceSession,
        event: Mapping[str, Any],
    ) -> None:
        synthesizer = getattr(self.provider, "synthesize_sideband_confirmation", None)
        if not callable(synthesizer):
            return
        confirmation = synthesizer(event=event)
        if isinstance(confirmation, Mapping):
            self._dispatch_sideband_confirmations(session, confirmation)

    async def _send_tool_response(
        self,
        session: LiveVoiceSession,
        *,
        call_id: str,
        output: Any = None,
        safe_error: str | None = None,
    ) -> None:
        """Complete a provider function call without exposing internal errors."""

        if not call_id or not self._sideband_is_established(session):
            return
        if safe_error is not None:
            payload: Any = {"error": safe_error}
        else:
            payload = output
        try:
            await self._send_sideband_event(
                session,
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(payload, ensure_ascii=False, default=str),
                    },
                },
            )
            await self._send_sideband_event(session, {"type": "response.create"})
        except Exception as exc:
            logger.warning("Live Voice sideband tool response failed: %s", type(exc).__name__)

    async def _close_late_provider_call(self, call_id: str | None) -> None:
        """Close a provider call returned after its runtime was terminalized."""

        normalized_call_id = str(call_id or "").strip()
        if not normalized_call_id:
            return
        close_sideband = getattr(self.provider, "close_sideband", None)
        if close_sideband is not None:
            try:
                await _maybe_await(close_sideband(normalized_call_id))
            except Exception as exc:
                logger.debug("Live Voice late provider sideband close skipped: %s", type(exc).__name__)
        hangup_call = getattr(self.provider, "hangup_call", None)
        if hangup_call is not None:
            try:
                await asyncio.wait_for(
                    _maybe_await(hangup_call(normalized_call_id)),
                    timeout=PROVIDER_HANGUP_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                logger.warning("Live Voice late provider hangup skipped: %s", type(exc).__name__)

    def _schedule_cancelled_connect_cleanup(
        self,
        session: LiveVoiceSession,
        *,
        late_call_id: str | None = None,
    ) -> None:
        """Terminalize cancellation synchronously, then clean provider async.

        Cancellation may arrive while another coroutine owns the lifecycle
        lock. Waiting for that lock would leave the HTTP task stuck. Dict/status
        updates are event-loop atomic;
        perform them immediately and let a shielded background task await
        provider/task shutdown independently.
        """

        installed_call_id = str(session.call_id or "").strip() or None
        session.status = "failed"
        session._sideband_provenance = None
        session._connect_in_progress = False
        self._sessions.pop(session.id, None)

        async def _cleanup() -> None:
            if installed_call_id:
                session.call_id = installed_call_id
                if self._sideband_is_established(session):
                    try:
                        await self._send_sideband_event(
                            session, {"type": "response.cancel"}
                        )
                    except Exception:
                        pass
            await self._cancel_sideband_task(session)
            session.call_id = None
            if late_call_id and late_call_id != installed_call_id:
                await self._close_late_provider_call(late_call_id)
            audit_pending = not await self._complete_agent_run(
                session.agent_run_id,
                result={
                    "source": "live_voice",
                    "live_session_id": session.id,
                    "event_source": EVENT_SOURCE_SIDEBAND,
                    "status": "cancelled",
                },
                message="Live Voice connect cancelled",
            )
            await self._broadcast(
                session,
                {
                    "event_type": "live_voice.failed",
                    "event_source": "server",
                    "agent_run_id": session.agent_run_id,
                    "audit_pending": audit_pending,
                },
            )

        cleanup_task = asyncio.create_task(
            _cleanup(), name=f"live-voice-cancel-cleanup-{session.id}"
        )
        self._connect_cleanup_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(self._connect_cleanup_tasks.discard)

    async def _mark_sideband_failed(
        self,
        session: LiveVoiceSession,
        *,
        lock_held: bool = False,
    ) -> None:
        """Terminalize a provider-owned session exactly once.

        Callers that already hold ``session._connect_lock`` (connect setup and
        TTL/close transitions) pass ``lock_held=True`` to avoid self-deadlock.
        Other paths acquire the same lifecycle lock so a concurrent /end or
        provider event cannot resurrect call/provenance state.
        """

        if not lock_held:
            async with session._connect_lock:
                await self._mark_sideband_failed(session, lock_held=True)
            return
        was_active = session.status == "active"
        session.status = "failed"
        if not was_active:
            return
        # A provider disconnect is terminal for this runtime id. Remove it
        # immediately rather than retaining a failed session until the normal
        # active-session TTL sweep. Keep only the local
        # object while the disconnect audit finishes; subsequent GET/events
        # resolve to 404 from the registry.
        session._sideband_provenance = None
        session._connect_in_progress = False
        async with self._lock:
            self._sessions.pop(session.id, None)
        if self._sideband_is_established(session):
            try:
                await self._send_sideband_event(session, {"type": "response.cancel"})
            except Exception:
                pass
        await self._cancel_sideband_task(session)
        session.call_id = None
        audit_pending = not await self._complete_agent_run(
            session.agent_run_id,
            result={
                "source": "live_voice",
                "live_session_id": session.id,
                "event_source": EVENT_SOURCE_SIDEBAND,
            },
            message="Live Voice sideband disconnected",
        )
        await self._broadcast(
            session,
            {
                "event_type": "live_voice.failed",
                "event_source": "server",
                "agent_run_id": session.agent_run_id,
                "audit_pending": audit_pending,
            },
        )

    async def connect_unified_call(
        self,
        session_id: str,
        actor: LiveVoiceActor,
        *,
        sdp: str,
        instructions: str | None = None,
    ) -> Mapping[str, Any]:
        session = await self._active_session(session_id, actor)
        offer = _validate_sdp_offer(sdp)
        # Reserve the one connect transition, but deliberately release the
        # lifecycle lock while waiting on provider HTTP. This lets /end or TTL
        # terminalize the runtime; the provider result is validated against the
        # status/registry before it can install call/sideband state.
        try:
            async with session._connect_lock:
                async with self._lock:
                    registered = self._sessions.get(session.id) is session
                if (
                    session.status != "active"
                    or not registered
                    or session._connect_in_progress
                    or session.call_id
                    or (
                        session.id in self._sideband_tasks
                        and not self._sideband_tasks[session.id].done()
                    )
                ):
                    raise LiveVoiceError(
                        "Live Voice session is no longer available", status_code=409
                    )
                session._connect_in_progress = True
        except asyncio.CancelledError:
            self._schedule_cancelled_connect_cleanup(session)
            raise

        tools = self._tool_definitions()
        try:
            async with session._connect_lock:
                async with self._lock:
                    registered = self._sessions.get(session.id) is session
                if (
                    session.status != "active"
                    or not registered
                    or not session._connect_in_progress
                ):
                    session._connect_in_progress = False
                    raise LiveVoiceError(
                        "Live Voice session is no longer active", status_code=409
                    )
        except asyncio.CancelledError:
            # The second lifecycle-lock acquisition occurs after the start
            # marker is reserved. Cancellation here must release that marker
            # and secret/registry state without awaiting the held lock.
            self._schedule_cancelled_connect_cleanup(session)
            raise
        # ``instructions`` is accepted for source compatibility but is
        # intentionally ignored. Only the value captured at session start can
        # cross the provider boundary.
        provider_actor = self._provider_actor(actor, session)
        try:
            self._privacy_preflight(
                provider_actor,
                session_id=session.conversation_session_id,
                project_id=session.project_id,
                session_context=session.session_context,
                project_metadata=session.project_metadata,
            )
        except LiveVoiceError:
            async with session._connect_lock:
                session._connect_in_progress = False
            await self._mark_sideband_failed(session)
            raise
        try:
            result = await self._call_provider(
                "create_unified_call",
                actor=provider_actor,
                session=session,
                sdp=offer,
                model=session.model,
                voice=session.voice,
                output_modalities=(
                    ["text"]
                    if session.mode == "realtime_character_tts"
                    else ["audio"]
                ),
                instructions=session._instructions,
                tools=tools,
                tool_choice="auto" if tools else "none",
                session_context=session.session_context,
                project_metadata=session.project_metadata,
            )
        except asyncio.CancelledError:
            self._schedule_cancelled_connect_cleanup(session)
            raise
        except LiveVoiceError as exc:
            async with session._connect_lock:
                session._connect_in_progress = False
                async with self._lock:
                    terminal = (
                        session.status != "active"
                        or self._sessions.get(session.id) is not session
                    )
            if terminal:
                raise LiveVoiceError(
                    "Live Voice session is no longer active", status_code=409
                ) from exc
            await self._mark_sideband_failed(session)
            raise
        except Exception as exc:
            async with session._connect_lock:
                session._connect_in_progress = False
                async with self._lock:
                    terminal = (
                        session.status != "active"
                        or self._sessions.get(session.id) is not session
                    )
            if terminal:
                raise LiveVoiceError(
                    "Live Voice session is no longer active", status_code=409
                ) from exc
            await self._mark_sideband_failed(session)
            raise LiveVoiceProviderError(
                _safe_provider_failure_message(exc), status_code=502
            ) from exc

        answer_sdp_raw = (
            str(result.get("sdp") or "") if isinstance(result, Mapping) else ""
        )
        answer_sdp = answer_sdp_raw.strip()
        result_call_id = (
            str(result.get("call_id") or "").strip() if isinstance(result, Mapping) else ""
        )
        try:
            async with session._connect_lock:
                session._connect_in_progress = False
                async with self._lock:
                    terminal = (
                        session.status != "active"
                        or self._sessions.get(session.id) is not session
                    )
                if terminal:
                    # A late provider result must never become a live runtime.
                    await self._close_late_provider_call(result_call_id)
                    raise LiveVoiceError(
                        "Live Voice session is no longer active", status_code=409
                    )
                if not isinstance(result, Mapping) or not answer_sdp:
                    if result_call_id:
                        session.call_id = result_call_id
                    await self._mark_sideband_failed(session, lock_held=True)
                    raise LiveVoiceProviderError("Realtime SDP answer is missing", status_code=502)
                # Capture call_id before validating it so a malformed provider
                # response can still close the provider-side call/sideband.
                session.call_id = result_call_id or None
                if not session.call_id:
                    await self._mark_sideband_failed(session, lock_held=True)
                    raise LiveVoiceProviderError(
                        "Realtime call_id is missing from provider response", status_code=502
                    )
                session.last_activity_at = datetime.now(timezone.utc)
                # Unified WebRTC calls expose a Location/call_id. Keep the
                # provider-owned sideband listener server-side so browser events
                # cannot execute tools directly.
                session._sideband_provenance = object()
                if hasattr(self.provider, "iter_sideband_events"):
                    self._sideband_tasks[session.id] = asyncio.create_task(
                        self._run_sideband(session, actor),
                        name=f"live-voice-sideband-{session.id}",
                    )
                    # Give the listener one scheduling turn to establish its
                    # receive half before the first server-side session.update.
                    try:
                        await asyncio.sleep(0)
                    except asyncio.CancelledError:
                        raise
                    listener = self._sideband_tasks.get(session.id)
                    if listener is not None and listener.done():
                        await self._mark_sideband_failed(session, lock_held=True)
                        raise LiveVoiceProviderError(
                            "Realtime sideband disconnected during setup", status_code=502
                        )
                try:
                    # Server-side tools/tool_choice are applied after the call is
                    # connected. This is queued on the same persistent sideband
                    # writer as function_call_output events.
                    try:
                        from .voice_sessions.openai_realtime_runtime import (
                            build_realtime_session_update,
                        )
                        from .voice_sessions.models import VoiceSessionMode
                        from .voice_sessions.policy import VoiceSessionPolicyResolver

                        policy = session.policy
                        if policy is None:
                            mode_raw = session.mode or VoiceSessionMode.REALTIME_NATIVE.value
                            policy = VoiceSessionPolicyResolver.resolve(
                                config=self.config,
                                actor=actor,
                                requested_mode=mode_raw,
                                legacy_model=session.model,
                                legacy_voice=session.voice,
                                legacy_instructions=session._instructions,
                            )
                            session.policy = policy
                            session.mode = str(policy.mode)
                        update_session = build_realtime_session_update(
                            policy,
                            tools=tools,
                            tool_choice="auto" if tools else "none",
                            instructions=session._instructions,
                        )
                    except Exception:
                        update_session = {
                            "type": "realtime",
                            "output_modalities": (
                                ["text"]
                                if session.mode == "realtime_character_tts"
                                else ["audio"]
                            ),
                            "tools": tools,
                            "tool_choice": "auto" if tools else "none",
                        }
                        if session._instructions:
                            update_session["instructions"] = session._instructions
                    await self._send_sideband_event(
                        session,
                        {"type": "session.update", "session": update_session},
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._mark_sideband_failed(session, lock_held=True)
                    raise LiveVoiceProviderError(
                        "Realtime sideband setup failed", status_code=502
                    ) from exc

                try:
                    await self._record_run_event(
                        session,
                        "live_voice.sideband.ready",
                        status="running",
                        message="Realtime call connected",
                        payload={
                            "source": "live_voice",
                            "call_id": session.call_id,
                            "provider": session.provider,
                        },
                    )
                except asyncio.CancelledError:
                    raise
                except LiveVoiceProviderError:
                    await self._mark_sideband_failed(session, lock_held=True)
                    raise
                response = {
                    "sdp": answer_sdp_raw,
                    "call_id": session.call_id,
                    "session": session.to_dict(),
                }
            return response
        except asyncio.CancelledError:
            # Cancellation can arrive while waiting for the lifecycle lock or
            # during sideband/session.update/audit setup after the provider has
            # already allocated a call. Mark terminal synchronously and let a
            # background cleanup task await provider shutdown; do not wait for
            # a lock held by another request before returning cancellation.
            self._schedule_cancelled_connect_cleanup(
                session, late_call_id=result_call_id
            )
            raise

    async def _restore_sideband_event_dict(
        self,
        session: LiveVoiceSession,
        event: Mapping[str, Any],
    ) -> dict[str, Any]:
        restored = dict(event)
        restorer = getattr(self.provider, "restore_sideband_event", None)
        if callable(restorer) and session.call_id:
            try:
                payload = await _maybe_await(
                    restorer(call_id=session.call_id, event=restored)
                )
                if isinstance(payload, Mapping):
                    restored = dict(payload)
            except Exception:
                logger.debug(
                    "Live Voice transcript alias restore skipped", exc_info=True
                )
        return restored

    async def _stop_sideband_processor(self, session: LiveVoiceSession) -> None:
        queue = self._sideband_event_queues.pop(session.id, None)
        if queue is not None:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        processor = self._sideband_processor_tasks.pop(session.id, None)
        await self._cancel_task(processor)

    async def _process_sideband_event_queue(
        self,
        session: LiveVoiceSession,
        actor: LiveVoiceActor,
        queue: asyncio.Queue[Any | None],
        provenance: object,
    ) -> None:
        while True:
            event = await queue.get()
            if event is None:
                return
            try:
                await self.handle_event(
                    session.id,
                    actor,
                    event,
                    source=EVENT_SOURCE_SIDEBAND,
                    provenance=provenance,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Live Voice sideband event processing failed")
                await self._mark_sideband_failed(session)
                return
            if session.status != "active":
                return

    async def _run_sideband_receiver_loop(
        self,
        session: LiveVoiceSession,
        events: AsyncIterable[Mapping[str, Any]],
        queue: asyncio.Queue[Any | None],
    ) -> None:
        try:
            async for raw_event in events:
                if not isinstance(raw_event, Mapping):
                    continue
                event = await self._restore_sideband_event_dict(session, raw_event)
                self._dispatch_sideband_confirmations(session, event)
                await queue.put(event)
                if session.status != "active":
                    break
        finally:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                await queue.put(None)

    async def _start_sideband_processor(
        self,
        session: LiveVoiceSession,
        actor: LiveVoiceActor,
        provenance: object,
    ) -> asyncio.Queue[Any | None]:
        queue: asyncio.Queue[Any | None] = asyncio.Queue(maxsize=512)
        self._sideband_event_queues[session.id] = queue
        processor = asyncio.create_task(
            self._process_sideband_event_queue(session, actor, queue, provenance),
            name=f"live-voice-sideband-processor-{session.id}",
        )
        self._sideband_processor_tasks[session.id] = processor
        return queue

    async def _run_sideband(
        self,
        session: LiveVoiceSession,
        actor: LiveVoiceActor,
    ) -> None:
        iterator_factory = getattr(self.provider, "iter_sideband_events", None)
        if iterator_factory is None or not session.call_id:
            return
        provenance = session._sideband_provenance
        queue = await self._start_sideband_processor(session, actor, provenance)
        try:
            events = iterator_factory(call_id=session.call_id)
            await self._run_sideband_receiver_loop(session, events, queue)
            if session.status == "active":
                await self._mark_sideband_failed(session)
                await self._record_run_event(
                    session,
                    "live_voice.sideband.disconnected",
                    status="failed",
                    message="Realtime sideband disconnected",
                    payload={
                        "source": "live_voice",
                        "event_source": EVENT_SOURCE_SIDEBAND,
                        "call_id": session.call_id,
                    },
                )
        except asyncio.CancelledError:
            # An externally cancelled listener is indistinguishable from a
            # provider disconnect unless the owning session is already
            # terminal or removed by the normal service-close path.
            async with self._lock:
                registered_active = (
                    session.status == "active"
                    and self._sessions.get(session.id) is session
                )
            if registered_active:
                await self._mark_sideband_failed(session)
            raise
        except Exception as exc:
            logger.warning("Live Voice sideband disconnected: %s", type(exc).__name__)
            if session.status == "active":
                await self._mark_sideband_failed(session)
            await self._record_run_event(
                session,
                "live_voice.sideband.disconnected",
                status="failed",
                message="Realtime sideband disconnected",
                payload={
                    "source": "live_voice",
                    "event_source": EVENT_SOURCE_SIDEBAND,
                    "call_id": session.call_id,
                },
            )
        finally:
            await self._stop_sideband_processor(session)
            current = self._sideband_tasks.get(session.id)
            if current is asyncio.current_task():
                self._sideband_tasks.pop(session.id, None)

    async def consume_sideband(
        self,
        session_id: str,
        actor: LiveVoiceActor,
        events: AsyncIterable[Mapping[str, Any]],
    ) -> None:
        """Consume provider sideband events until disconnect/stream end."""

        session = await self._active_session(session_id, actor)
        provenance = session._sideband_provenance
        if provenance is None:
            provenance = object()
            session._sideband_provenance = provenance
        queue = await self._start_sideband_processor(session, actor, provenance)
        try:
            await self._run_sideband_receiver_loop(session, events, queue)
            if session.status == "active":
                await self._mark_sideband_failed(session)
        except asyncio.CancelledError:
            async with self._lock:
                registered_active = (
                    session.status == "active"
                    and self._sessions.get(session.id) is session
                )
            if registered_active:
                await self._mark_sideband_failed(session)
            raise
        except Exception:
            await self._mark_sideband_failed(session)
            raise
        finally:
            await self._stop_sideband_processor(session)

    async def validate_audio_websocket(
        self,
        voice_session_id: str,
        actor: LiveVoiceActor,
    ) -> LiveVoiceSession:
        from .voice_sessions.models import VoiceSessionMode

        session = await self._active_session(voice_session_id, actor)
        if session.mode != VoiceSessionMode.REALTIME_CHARACTER_TTS.value:
            raise LiveVoiceError(
                "Audio transport is only available for realtime_character_tts mode",
                status_code=400,
            )
        transport = getattr(session, "audio_transport", None)
        if transport is None:
            raise LiveVoiceError(
                "Audio transport is not configured for this session",
                status_code=409,
            )
        return session

    async def run_audio_websocket(
        self,
        session: LiveVoiceSession,
        websocket: Any,
    ) -> None:
        import json

        transport = getattr(session, "audio_transport", None)
        if transport is None:
            raise LiveVoiceError(
                "Audio transport is not configured for this session",
                status_code=409,
            )

        async def send_json(payload: dict[str, Any]) -> None:
            await websocket.send_json(payload)

        async def send_binary(payload: bytes) -> None:
            await websocket.send_bytes(payload)

        transport.bind_connection(send_json=send_json, send_binary=send_binary)
        generation = getattr(session, "voice_generation", None)
        if generation is not None:
            await transport.activate_generation(generation.generation_id)
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                text = message.get("text")
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    await transport.handle_client_message(payload)
        finally:
            transport.unbind_connection()
            await self._handle_audio_websocket_disconnect(session)

    @staticmethod
    def _bind_assistant_item_id(
        generation: Any | None,
        event: Mapping[str, Any],
        *,
        item: Mapping[str, Any] | None = None,
    ) -> None:
        if generation is None:
            return
        item_id = str(event.get("item_id") or "").strip()
        if not item_id and isinstance(item, Mapping):
            item_id = str(item.get("id") or "").strip()
        if item_id:
            generation.item_id = item_id

    async def _handle_audio_websocket_disconnect(
        self,
        session: LiveVoiceSession,
    ) -> None:
        from .voice_sessions.generation import GenerationPhase
        from .voice_sessions.models import VoiceSessionMode

        if session.mode != VoiceSessionMode.REALTIME_CHARACTER_TTS.value:
            return
        if session.status != "active":
            return
        generation = getattr(session, "voice_generation", None)
        if generation is not None and generation.phase not in {
            GenerationPhase.COMPLETED,
            GenerationPhase.FAILED,
            GenerationPhase.INTERRUPTED,
        }:
            generation.phase = GenerationPhase.FAILED
        await self._mark_sideband_failed(session)

    def _app_config_dict(self) -> dict[str, Any] | None:
        config = self.config
        if config is None:
            return None
        if isinstance(config, dict):
            return config
        nested = getattr(config, "config", None)
        return nested if isinstance(nested, dict) else None

    def _build_character_tts_output(
        self,
        *,
        session: LiveVoiceSession,
        policy: Any,
        transport: Any,
    ) -> Any:
        from .voice_sessions.character_tts_output import CharacterTTSOutput

        async def on_playback_complete(generation: Any) -> None:
            await self._commit_character_tts_assistant_message(
                session=session,
                generation=generation,
                event={"type": "voice.playback.complete"},
            )

        async def on_output_failure(generation: Any, reason: str) -> None:
            logger.warning(
                "Character TTS output failed for session %s: %s",
                session.id,
                reason,
            )
            await self._mark_sideband_failed(session)

        character_output = CharacterTTSOutput(
            policy=policy,
            character_name=session.character_name or "assistant",
            transport=transport,
            app_config=self.config,
            on_playback_complete=on_playback_complete,
            on_output_failure=on_output_failure,
        )

        async def on_ack(generation_id: str, sequence: int) -> None:
            await character_output.on_segment_acked(generation_id, sequence)

        transport.set_on_ack(on_ack)
        return character_output

    async def _fail_reconciliation(
        self,
        session: LiveVoiceSession,
        generation: Any,
        *,
        message: str,
    ) -> None:
        from .voice_sessions.generation import GenerationPhase

        generation.phase = GenerationPhase.FAILED
        logger.warning("Live Voice reconciliation failed: %s", message)
        await self._mark_sideband_failed(session)

    async def _send_confirmed_sideband_delete(
        self,
        session: LiveVoiceSession,
        *,
        item_id: str,
    ) -> None:
        outbound = {"type": "conversation.item.delete", "item_id": item_id}
        future = self._register_sideband_confirmation(
            session,
            expect_type="conversation.item.deleted",
            matcher=lambda event: str(event.get("item_id") or "").strip() == item_id,
        )
        try:
            await self._send_sideband_event(session, outbound)
            await self._emit_mock_sideband_confirmation_if_available(session, outbound)
            await asyncio.wait_for(future, timeout=SIDEBAND_SETUP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            if not future.done():
                future.cancel()
            raise LiveVoiceProviderError(
                "Realtime sideband delete confirmation timed out",
                status_code=502,
            ) from exc
        finally:
            session._sideband_confirmations[:] = [
                item for item in session._sideband_confirmations if item[0] is not future
            ]

    async def _send_confirmed_sideband_create(
        self,
        session: LiveVoiceSession,
        *,
        spoken_text: str,
    ) -> None:
        replacement_item_id = f"msg_aoi_{uuid.uuid4().hex}"
        outbound = {
            "type": "conversation.item.create",
            "item": {
                "id": replacement_item_id,
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": spoken_text,
                    }
                ],
            },
        }
        future = self._register_sideband_confirmation(
            session,
            expect_type="conversation.item.created",
            matcher=lambda event, expected_id=replacement_item_id: str(
                (event.get("item") or {}).get("id") or ""
            ).strip()
            == expected_id,
        )
        try:
            await self._send_sideband_event(session, outbound)
            await self._emit_mock_sideband_confirmation_if_available(session, outbound)
            await asyncio.wait_for(future, timeout=SIDEBAND_SETUP_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            if not future.done():
                future.cancel()
            raise LiveVoiceProviderError(
                "Realtime sideband create confirmation timed out",
                status_code=502,
            ) from exc
        finally:
            session._sideband_confirmations[:] = [
                item for item in session._sideband_confirmations if item[0] is not future
            ]

    async def _reconcile_realtime_assistant_item(
        self,
        session: LiveVoiceSession,
        generation: Any,
        spoken_text: str,
    ) -> bool:
        item_id = str(getattr(generation, "item_id", None) or "").strip()
        full_text = str(getattr(generation, "full_text", "") or "")
        normalized_spoken = str(spoken_text or "").strip()

        if not item_id:
            if not full_text:
                return True
            await self._fail_reconciliation(
                session,
                generation,
                message="missing assistant item_id",
            )
            return False

        if not self._sideband_is_established(session):
            await self._fail_reconciliation(
                session,
                generation,
                message="sideband unavailable",
            )
            return False

        if normalized_spoken == full_text:
            return True

        try:
            await self._send_confirmed_sideband_delete(session, item_id=item_id)
            if normalized_spoken:
                await self._send_confirmed_sideband_create(
                    session,
                    spoken_text=normalized_spoken,
                )
            return True
        except LiveVoiceProviderError:
            await self._fail_reconciliation(
                session,
                generation,
                message="provider confirmation failed",
            )
            return False
        except Exception:
            logger.exception("Live Voice assistant item reconciliation failed")
            await self._fail_reconciliation(
                session,
                generation,
                message="unexpected reconciliation error",
            )
            return False

    async def _commit_character_tts_assistant_message(
        self,
        *,
        session: LiveVoiceSession,
        generation: Any,
        event: Mapping[str, Any],
    ) -> str | None:
        from .voice_sessions.generation import GenerationPhase

        if generation.phase in {
            GenerationPhase.INTERRUPTED,
            GenerationPhase.FAILED,
        }:
            return None
        if generation.metadata.get("durable_committed"):
            return None
        transcript = str(getattr(generation, "full_text", "") or "").strip()
        if not transcript:
            generation.phase = GenerationPhase.COMPLETED
            return None
        message_id = await self._persist_transcript(
            session=session,
            role="assistant",
            transcript=transcript,
            event=event,
            event_source=EVENT_SOURCE_SIDEBAND,
            metadata={
                "generated_text": transcript,
                "spoken_text": transcript,
                "interrupted": False,
                "generation_id": generation.generation_id,
            },
        )
        generation.metadata["durable_committed"] = True
        generation.phase = GenerationPhase.COMPLETED
        return message_id

    async def interrupt_voice_session(
        self,
        session: LiveVoiceSession,
        *,
        spoken_prefix: str = "",
        partial_unknown: bool = False,
    ) -> dict[str, Any]:
        from .voice_sessions.models import VoiceSessionMode

        if session.mode == VoiceSessionMode.REALTIME_CHARACTER_TTS.value:
            if spoken_prefix or partial_unknown:
                generation = session.voice_generation
                if generation is not None and session._character_tts is not None:
                    from .voice_sessions.generation import interrupt_generation

                    await interrupt_generation(
                        generation,
                        lock=session._generation_lock,
                        spoken_prefix=spoken_prefix,
                        partial_unknown=partial_unknown,
                    )
            else:
                await self._interrupt_character_tts(session)
        elif session.policy is not None and session.policy.interrupt_response:
            if self._sideband_is_established(session):
                await self._send_sideband_event(session, {"type": "response.cancel"})
        return {"accepted": True, "session": session.to_dict()}

    async def interrupt_session(self, session_id: str, actor: LiveVoiceActor) -> dict[str, Any]:
        session = await self._active_session(session_id, actor)
        try:
            from .voice_sessions.models import VoiceSessionMode

            if session.mode == VoiceSessionMode.REALTIME_CHARACTER_TTS.value:
                await self._interrupt_character_tts(session)
        except Exception:
            logger.debug("Character TTS interrupt skipped", exc_info=True)
        if self._sideband_is_established(session):
            try:
                await self._send_sideband_event(session, {"type": "response.cancel"})
                await self._send_sideband_event(
                    session, {"type": "output_audio_buffer.clear"}
                )
            except Exception:
                logger.debug("Realtime interrupt sideband event failed", exc_info=True)
        return {"accepted": True, "session": session.to_dict()}

    async def _interrupt_character_tts(self, session: LiveVoiceSession) -> None:
        from .voice_sessions.generation import GenerationPhase, VoiceGenerationState, interrupt_generation
        from .voice_sessions.models import VoiceSessionMode

        if session.mode != VoiceSessionMode.REALTIME_CHARACTER_TTS.value:
            return
        generation = getattr(session, "voice_generation", None)
        if generation is None:
            generation = VoiceGenerationState.mint()
            session.voice_generation = generation
        character_output = session._character_tts
        spoken_prefix = ""
        partial_unknown = False
        if character_output is not None:
            await character_output.reset_for_interrupt()
            spoken_prefix, partial_unknown = character_output.spoken_prefix()
        transport = getattr(session, "audio_transport", None)
        if transport is not None:
            await transport.clear_generation(
                generation_id=generation.generation_id,
                reason="barge_in",
            )
        await interrupt_generation(
            generation,
            spoken_prefix=spoken_prefix,
            partial_unknown=partial_unknown,
            before_playback=not spoken_prefix,
        )
        reconciled = await self._reconcile_realtime_assistant_item(
            session,
            generation,
            generation.spoken_text,
        )
        if not reconciled:
            return
        if generation.spoken_text:
            await self._persist_transcript(
                session=session,
                role="assistant",
                transcript=generation.spoken_text,
                event={"type": "voice.interrupted"},
                event_source=EVENT_SOURCE_SIDEBAND,
                metadata={
                    "generated_text": generation.full_text,
                    "spoken_text": generation.spoken_text,
                    "interrupted": True,
                    "generation_id": generation.generation_id,
                    "playback_partial_unknown": partial_unknown,
                },
            )

    async def _handle_character_tts_sideband_event(
        self,
        session: LiveVoiceSession,
        event_type: str,
        event: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        from .voice_sessions.audio_transport import VoiceAudioTransportManager
        from .voice_sessions.generation import GenerationPhase, VoiceGenerationState
        from .voice_sessions.models import VoiceSessionMode

        if session.mode != VoiceSessionMode.REALTIME_CHARACTER_TTS.value:
            return None
        policy = session.policy
        if policy is None:
            return None
        if event_type == "input_audio_buffer.speech_started":
            await self._interrupt_character_tts(session)
            if self._sideband_is_established(session):
                await self._send_sideband_event(session, {"type": "response.cancel"})
            return {
                "accepted": True,
                "event_type": event_type,
                "event_source": EVENT_SOURCE_SIDEBAND,
                "session": session.to_dict(),
            }
        if session._character_tts is None:
            transport = VoiceAudioTransportManager(
                unacked_window=1,
                queue_maxsize=2,
            )
            session.audio_transport = transport
            session._character_tts = self._build_character_tts_output(
                session=session,
                policy=policy,
                transport=transport,
            )
        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, Mapping) and str(item.get("type") or "") == "message":
                self._bind_assistant_item_id(
                    session.voice_generation,
                    event,
                    item=item,
                )
            return {
                "accepted": True,
                "event_type": event_type,
                "event_source": EVENT_SOURCE_SIDEBAND,
                "session": session.to_dict(),
            }
        if event_type == "response.created":
            generation = VoiceGenerationState.mint(
                response_id=str(event.get("response", {}).get("id") or "")
                if isinstance(event.get("response"), Mapping)
                else None
            )
            session.voice_generation = generation
            session._character_tts.bind_generation(generation)
            transport = getattr(session, "audio_transport", None)
            if transport is not None:
                await transport.activate_generation(generation.generation_id)
            return {
                "accepted": True,
                "event_type": event_type,
                "event_source": EVENT_SOURCE_SIDEBAND,
                "session": session.to_dict(),
            }
        if event_type == "response.output_text.delta":
            delta = str(event.get("delta") or "")
            self._bind_assistant_item_id(session.voice_generation, event)
            await session._character_tts.push_text_delta(delta)
            return {
                "accepted": True,
                "event_type": event_type,
                "event_source": EVENT_SOURCE_SIDEBAND,
                "session": session.to_dict(),
            }
        if event_type == "response.output_text.done":
            self._bind_assistant_item_id(session.voice_generation, event)
            await session._character_tts.flush()
            return {
                "accepted": True,
                "event_type": event_type,
                "event_source": EVENT_SOURCE_SIDEBAND,
                "session": session.to_dict(),
            }
        if event_type == "response.done":
            generation = session.voice_generation
            if generation is not None and generation.phase != GenerationPhase.INTERRUPTED:
                await session._character_tts.flush()
                await session._character_tts.mark_response_complete()
            return {
                "accepted": True,
                "event_type": event_type,
                "event_source": EVENT_SOURCE_SIDEBAND,
                "session": session.to_dict(),
            }
        return None

    async def close_session(self, session_id: str, actor: LiveVoiceActor) -> dict[str, Any]:
        session = await self.get_session(session_id, actor)
        should_complete = False
        async with session._connect_lock:
            if session.status not in {"closed", "failed"}:
                # Mark terminal and clear the provenance before any awaited
                # AgentRun/broadcast work. In-flight provider events/tool calls
                # then fail their lifecycle recheck instead of running after
                # /end has taken effect.
                session.status = "closed"
                session.last_activity_at = datetime.now(timezone.utc)
                should_complete = True
            session._sideband_provenance = None
            session._connect_in_progress = False
            async with self._lock:
                if self._sessions.get(session.id) is session:
                    self._sessions.pop(session.id, None)
            await self._cancel_sideband_task(session)
            session.call_id = None
        audit_pending = False
        if should_complete:
            audit_pending = not await self._complete_agent_run(
                session.agent_run_id,
                result={"source": "live_voice", "live_session_id": session.id},
                message="Live Voice session closed",
            )
            await self._broadcast(
                session,
                {
                    "event_type": "session.closed",
                    "agent_run_id": session.agent_run_id,
                    "audit_pending": audit_pending,
                },
            )
        return {"success": True, "audit_pending": audit_pending, "session": session.to_dict()}

    async def close(self) -> None:
        cleanup_task = self._cleanup_task
        self._cleanup_task = None
        await self._cancel_task(cleanup_task)
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            sideband_tasks = list(self._sideband_tasks.values())
            self._sideband_tasks.clear()
            processor_tasks = list(self._sideband_processor_tasks.values())
            self._sideband_processor_tasks.clear()
            self._sideband_event_queues.clear()
        for task in sideband_tasks:
            await self._cancel_task(task)
        for task in processor_tasks:
            await self._cancel_task(task)
        for session in sessions:
            async with session._connect_lock:
                session.status = "closed"
                session._sideband_provenance = None
                session._connect_in_progress = False
                try:
                    await self._cancel_sideband_task(session)
                finally:
                    session.call_id = None
            await self._complete_agent_run(
                session.agent_run_id,
                result={"source": "live_voice", "live_session_id": session.id},
                message="Live Voice runtime shutdown",
            )
        # A cancelled connect may have already removed its session from the
        # registry while a provider call was still in flight. Await those
        # tracked cleanup tasks before closing the provider client so late call
        # IDs still receive a best-effort hangup during shutdown.
        pending_connect_cleanups = list(self._connect_cleanup_tasks)
        for task in pending_connect_cleanups:
            if task.done():
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except asyncio.TimeoutError:
                await self._cancel_task(task)
            except BaseException:
                await self._cancel_task(task)
        retry_task = self._terminalization_retry_task
        self._terminalization_retry_task = None
        tool_retry_task = self._tool_audit_retry_task
        self._tool_audit_retry_task = None
        event_retry_task = self._run_event_retry_task
        self._run_event_retry_task = None
        for task in (retry_task, tool_retry_task, event_retry_task):
            if task is None or task.done():
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
            except asyncio.TimeoutError:
                await self._cancel_task(task)
            except BaseException:
                # Preserve shutdown while still cancelling/awaiting the
                # background worker below.
                await self._cancel_task(task)
        async with self._lock:
            unresolved_audits = (
                len(self._pending_terminalizations)
                + len(self._pending_tool_audits)
                + len(self._pending_run_events)
            )
        if unresolved_audits:
            # No durable outbox adapter is available in this runtime. Keep the
            # sanitized in-memory ledger for diagnostics and report loudly
            # rather than claiming all AgentRun terminalization succeeded.
            logger.error(
                "Live Voice shutdown left %d AgentRun audit operations pending",
                unresolved_audits,
            )
        provider_close = getattr(self.provider, "close", None)
        if provider_close is not None:
            try:
                await _maybe_await(provider_close())
            except Exception as exc:
                logger.warning("Live Voice provider close failed: %s", type(exc).__name__)


# A short import-friendly alias for integrations that prefer ``live_voice``.
LiveVoiceRuntime = LiveVoiceService
LiveVoiceSessionService = LiveVoiceService
OpenAIRealtimeAdapter = OpenAIRealtimeProvider
MockLiveVoiceProvider = MockRealtimeProvider

__all__ = [
    "BROWSER_TELEMETRY_EVENT_TYPES",
    "DEFAULT_LIVE_VOICE_TOOLS",
    "DEFAULT_REALTIME_MODEL",
    "DEFAULT_REALTIME_MODELS",
    "DEFAULT_REALTIME_VOICE",
    "DEFAULT_REALTIME_VOICES",
    "EphemeralClientSecret",
    "EVENT_SOURCE_BROWSER",
    "EVENT_SOURCE_SIDEBAND",
    "SIDEBAND_SETUP_TIMEOUT_SECONDS",
    "LiveVoiceActor",
    "LiveVoiceError",
    "LiveVoiceNotFoundError",
    "LiveVoicePermissionError",
    "LiveVoiceProvider",
    "LiveVoiceProviderError",
    "LiveVoiceRuntime",
    "LiveVoiceSessionService",
    "LiveVoiceService",
    "LiveVoiceSession",
    "MockRealtimeProvider",
    "MockLiveVoiceProvider",
    "normalize_client_secret",
    "OpenAIRealtimeAdapter",
    "OpenAIRealtimeProvider",
]
