"""Native Grok Bot transport adapter.

This module is the small synchronous seam used by :mod:`grok_bot_chat_service`
when the in-process AgentLink implementation is selected.  The native link is
async, while the existing Chat transport protocol is synchronous, so the
adapter runs one coroutine at a time and projects the durable AgentLink
journal into the Grok Bot Chat transport outcome shape.

The adapter deliberately does not retain prompt text or a plaintext nonce.
The AgentLink journal stores only a nonce hash and the allow-listed baseline /
remote identifiers.  A deterministic turn id lets a retry recover that row
after a process restart and therefore never turns an uncertain send into a
second send.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import inspect
import re
import threading
import uuid
from typing import Any, Awaitable, Callable

from .agent_link.codes import LinkErrorCode
from .agent_link.contracts import Channel, Observation, Phase, TurnRef
from .agent_link.journal import JournalRecord, SQLiteAgentLinkJournal, valid_settlement
from .agent_link.service import AgentLinkService


# Domain separated from the nonce namespace in agent_link.contracts.  The
# operation id is a stable local ledger key, not an authentication credential.
_TURN_NAMESPACE = uuid.UUID("e0af9a5d-3c0d-57bb-b6a9-4c10b1921b17")
_OWNER_ID = "aoitalk-native"
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_STAGE = re.compile(r"^[a-z_]{1,32}$")
_SAFE_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")
_SAFE_REMOTE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")

_DELIVERY_ACCEPTED = "accepted"
_DELIVERY_REJECTED = "rejected"
_DELIVERY_UNKNOWN = "unknown"
_INSPECTION_OBSERVED = "observed"
_INSPECTION_PENDING = "pending"
_INSPECTION_UNKNOWN = "unknown"
_REMOTE_TERMINAL = "unsupported"
# Keep the native journal alive for the same long recovery window as the
# owner-scoped Chat receipt.  ``execute``/``reconcile`` only perform one
# observation each; this window belongs to background recovery and must not be
# confused with a foreground wait deadline.
GROK_RECOVERY_WINDOW_SECONDS = 14 * 24 * 60 * 60

_PENDING_CODES = {
    LinkErrorCode.GROK_REPLY_PENDING.value.upper(),
}


@dataclass(frozen=True)
class _NativeDiagnostic:
    """Allow-listed diagnostic fields from ``GrokLink.last_diagnostic``."""

    stage: str | None = None
    code: str | None = None
    status: int | None = None
    remote_request_id: str | None = None
    reply_entries_observed: int = 0
    tool_entries_observed: int = 0
    non_text_entries_observed: int = 0
    gap_reset: bool = False

    def as_mapping(self) -> dict[str, Any]:
        value: dict[str, Any] = {}
        if self.stage is not None:
            value["stage"] = self.stage
        if self.code is not None:
            value["code"] = self.code
        if self.status is not None:
            value["status"] = self.status
        if self.remote_request_id is not None:
            value["remote_request_id"] = self.remote_request_id
        value["reply_entries_observed"] = self.reply_entries_observed
        value["tool_entries_observed"] = self.tool_entries_observed
        value["non_text_entries_observed"] = self.non_text_entries_observed
        if self.gap_reset:
            value["gap_reset"] = True
        return value


def _safe_remote_id(value: object) -> str | None:
    if not isinstance(value, str) or not _SAFE_REMOTE_ID.fullmatch(value):
        return None
    lowered = value.casefold()
    if any(
        marker in lowered
        for marker in (
            "token",
            "secret",
            "credential",
            "password",
            "cookie",
            "authorization",
            "bearer",
            "api_key",
            "url",
            "uri",
            "path",
            "prompt",
            "body",
            "text",
            "nonce",
        )
    ):
        return None
    return value


def _safe_target(value: object) -> str | None:
    if not isinstance(value, str) or not _SAFE_TARGET.fullmatch(value):
        return None
    lowered = value.casefold()
    if any(
        marker in lowered
        for marker in (
            "token",
            "secret",
            "credential",
            "password",
            "cookie",
            "authorization",
            "bearer",
            "api_key",
            "url",
            "uri",
            "path",
            "prompt",
            "body",
            "text",
            "nonce",
        )
    ):
        return None
    return value


def _setting(settings: object, name: str) -> object:
    if isinstance(settings, Mapping):
        return settings.get(name)
    return getattr(settings, name, None)


def _safe_code(value: object, fallback: str = "LINK_INTERNAL") -> str:
    if isinstance(value, LinkErrorCode):
        return value.value.upper()
    if isinstance(value, str) and _SAFE_CODE.fullmatch(value):
        return value
    if isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,64}", value):
        return value.upper()
    return fallback


def _safe_status(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    return None


def _safe_count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10000:
        return value
    return 0


def _diagnostic(value: object) -> _NativeDiagnostic:
    """Project a GrokDiagnostic or mapping without reading exception text."""

    if isinstance(value, Mapping):
        raw = value
    else:
        # Some fakes expose a small dataclass with the same fields.  Attribute
        # reads are intentionally limited to the fixed diagnostic vocabulary.
        raw = {
            "stage": getattr(value, "stage", None),
            "code": getattr(value, "code", None),
            "status": getattr(value, "http_status", getattr(value, "status", None)),
            "remote_request_id": getattr(value, "request_id", None),
            "reply_entries_observed": getattr(value, "reply_entries", 0),
            "tool_entries_observed": getattr(value, "tool_entries", 0),
            "non_text_entries_observed": getattr(value, "non_text_entries", 0),
            "gap_reset": getattr(value, "gap_reset", False),
        }
    stage = raw.get("stage")
    if not isinstance(stage, str) or not _SAFE_STAGE.fullmatch(stage):
        stage = None
    code = raw.get("code")
    code = _safe_code(code, "") or None
    if code:
        # AgentLink codes are lower-case; Grok Bot Chat diagnostics use the
        # fixed upper-case code vocabulary.
        code = code.upper()
    status = _safe_status(raw.get("status", raw.get("http_status")))
    request_id = _safe_remote_id(raw.get("remote_request_id", raw.get("request_id")))
    return _NativeDiagnostic(
        stage=stage,
        code=code,
        status=status,
        remote_request_id=request_id,
        reply_entries_observed=_safe_count(
            raw.get("reply_entries_observed", raw.get("reply_entries", 0))
        ),
        tool_entries_observed=_safe_count(
            raw.get("tool_entries_observed", raw.get("tool_entries", 0))
        ),
        non_text_entries_observed=_safe_count(
            raw.get("non_text_entries_observed", raw.get("non_text_entries", 0))
        ),
        gap_reset=raw.get("gap_reset") is True,
    )


def _correlation(record: JournalRecord | None) -> dict[str, str] | None:
    if record is None:
        return None
    values: dict[str, str] = {}
    message_id = _safe_remote_id(record.remote_ids.get("message_id"))
    request_id = _safe_remote_id(record.remote_ids.get("request_id"))
    root_id = _safe_remote_id(record.remote_ids.get("root_id"))
    if message_id is not None:
        values["request_message_id"] = message_id
    if request_id is not None:
        values["request_id"] = request_id
    if root_id is not None:
        values["root_id"] = root_id
    return values or None


def _phase_delivery(phase: Phase | str | None) -> str:
    try:
        normalized = Phase(phase) if phase is not None else None
    except (TypeError, ValueError):
        normalized = None
    if normalized in {Phase.SENT, Phase.REPLIED, Phase.FAILED}:
        return _DELIVERY_ACCEPTED
    if normalized is Phase.REJECTED:
        return _DELIVERY_REJECTED
    return _DELIVERY_UNKNOWN


def _phase_value(phase: Phase | str | None) -> str | None:
    try:
        return Phase(phase).value if phase is not None else None
    except (TypeError, ValueError):
        return None


def _turn_id(operation_id: str) -> str:
    # UUID5 accepts arbitrary operation strings and gives the same native
    # nonce after a process restart.  The operation id itself is never logged.
    return str(uuid.uuid5(_TURN_NAMESPACE, operation_id))


async def _await_if_needed(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


class _PersistentAsyncLoop:
    """Run all HTTP/client work for one transport on one stable event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    def _start(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._closed:
                raise RuntimeError("native transport is closed")
            if self._thread is None:
                self._ready.clear()
                self._thread = threading.Thread(
                    target=self._run_loop,
                    name="grok-native-transport-loop",
                    daemon=True,
                )
                self._thread.start()
        if not self._ready.wait(timeout=10.0):
            raise RuntimeError("native transport loop did not start")
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("native transport loop is unavailable")
        return loop

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            asyncio.set_event_loop(None)

    def run(self, awaitable: Awaitable[Any]) -> Any:
        loop = self._start()
        if threading.current_thread() is self._thread:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise RuntimeError("native transport cannot synchronously re-enter its loop")
        future = asyncio.run_coroutine_threadsafe(awaitable, loop)
        return future.result()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10.0)


class GrokBotTransport:
    """Synchronous ``GrokBotTransport`` protocol implementation for GrokLink.

    ``link`` and ``service`` are injectable seams.  Supplying only ``client``
    creates a ``GrokLink`` pinned to ``settings.target_identity``; no agent
    discovery is needed.  Supplying a journal without a service creates the
    standard ``AgentLinkService`` around that journal.  Tests can therefore
    inject all three objects and never touch a profile, DPAPI, or network.
    """

    def __init__(
        self,
        settings: Any,
        *,
        link: Any | None = None,
        service: AgentLinkService | None = None,
        journal: SQLiteAgentLinkJournal | None = None,
        client: Any | None = None,
        target_agent_id: str | None = None,
        agent_resolver: Callable[[object], str] | None = None,
        service_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        configured_target = _setting(settings, "target_identity")
        if configured_target is None:
            configured_target = _setting(settings, "anchor_target")
        if configured_target is None:
            configured_target = target_agent_id
        target = _safe_target(configured_target)
        if target is None:
            raise ValueError("native Grok target is invalid")
        if target_agent_id is not None and target_agent_id != target:
            raise ValueError("native Grok target does not match settings")
        self._target = target

        self.link = link
        self.client = client
        self._agent_resolver = agent_resolver
        self._runner = _PersistentAsyncLoop()
        self._closed = False

        if journal is None and service is not None:
            journal = getattr(service, "journal", None)
        if journal is None:
            journal = SQLiteAgentLinkJournal()
        if service is None:
            kwargs = dict(service_kwargs or {})
            kwargs.setdefault("recovery_window_s", GROK_RECOVERY_WINDOW_SECONDS)
            service = AgentLinkService(journal, **kwargs)
        self.service = service
        self.journal = journal if journal is not None else getattr(service, "journal", None)
        if not isinstance(self.journal, SQLiteAgentLinkJournal):
            # A fake journal is useful for adapter tests, so retain duck typing
            # here rather than enforcing the concrete class at runtime.
            if self.journal is None or not callable(getattr(self.journal, "get", None)):
                raise ValueError("native Grok journal is required")

    def _build_default_client(self) -> Any:
        """Construct the real in-process client only when native send is used."""

        # Keep profile/DPAPI/HTTP dependencies lazy: fake link tests and
        # anchor checks never read the desktop profile or open a transport.
        from .agent_link.grok_native import (
            GrokGatewayClient,
            HttpxTransport,
            WindowsDPAPIUnprotector,
            WindowsProfileReader,
        )

        return GrokGatewayClient(
            WindowsProfileReader(),
            WindowsDPAPIUnprotector(),
            HttpxTransport(),
            expected_account_identity=_setting(self.settings, "account_identity"),
            expected_team_identity=_setting(self.settings, "team_identity"),
        )

    def _ensure_link(self) -> Any:
        if self.link is not None:
            return self.link
        if self.client is None:
            self.client = self._build_default_client()
        # Keep the fake-only import path free of HTTP/profile optional
        # dependencies.  Production construction loads GrokLink only when a
        # real client was explicitly supplied or built above.
        from .agent_link.grok_native.link import GrokLink

        self.link = GrokLink(
            self.client,
            target_agent_id=self._target,
            agent_resolver=self._agent_resolver,
        )
        return self.link

    def anchor_target(self) -> str:
        """Return the admitted target identity without performing I/O."""

        return self._target

    def _turn(self, operation_id: str) -> TurnRef:
        return TurnRef(
            turn_id=_turn_id(operation_id),
            channel=Channel.GROK_BOT,
            owner_id=_OWNER_ID,
            thread_ref=self._target,
        )

    def _record(self, operation_id: str) -> JournalRecord | None:
        value = self.journal.get(_turn_id(operation_id))
        return value

    def _last_diagnostic(self) -> _NativeDiagnostic:
        return _diagnostic(
            getattr(self.link, "last_diagnostic", None) if self.link is not None else None
        )

    @staticmethod
    def _reply_text(observation: object, record: JournalRecord | None) -> str:
        value = getattr(observation, "reply_text", None)
        if isinstance(value, str) and value:
            return value
        # A reopened REPLIED row may be observed by a fake that only returns
        # the terminal phase.  Native journal rows intentionally do not store
        # body text, so never invent or recover text here.
        return ""

    async def _prepare_and_send(self, turn: TurnRef, prompt: str) -> Any:
        # Construct the client and touch its async resources on the runner's
        # stable loop, including for the first operation.
        return await _await_if_needed(
            self.service.prepare_and_send(self._ensure_link(), turn, prompt)
        )

    async def _observe_once(self, turn: TurnRef) -> Any:
        return await _await_if_needed(
            self.service.observe_once(self._ensure_link(), turn)
        )

    def _outcome(
        self,
        operation_id: str,
        *,
        record: JournalRecord | None,
        receipt_phase: Phase | str | None = None,
        observation: Observation | None = None,
        diagnostic: _NativeDiagnostic | None = None,
        send_attempted: bool | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        phase = record.phase if record is not None else receipt_phase
        proof = self._settlement(operation_id, record)
        terminal = {"success": "completed", "cancelled": "cancelled", "error": "error"}.get(proof)
        delivery = _phase_delivery(phase)
        diag = diagnostic or self._last_diagnostic()
        text = self._reply_text(observation, record)

        observation_code = getattr(observation, "code", None)
        safe_observation_code = _safe_code(observation_code, "") or None
        if error_code is None:
            error_code = safe_observation_code or diag.code

        if terminal == "completed":
            error_code = _safe_code(record.code, "") or None if record and record.code else None

        if observation is not None:
            complete = getattr(observation, "complete", False) is True
            observation_phase = _phase_value(getattr(observation, "phase", None))
        else:
            complete = False
            observation_phase = None

        if text or terminal:
            inspection = _INSPECTION_OBSERVED
        elif diag.reply_entries_observed or diag.tool_entries_observed or diag.non_text_entries_observed:
            inspection = _INSPECTION_OBSERVED
        elif safe_observation_code in _PENDING_CODES:
            inspection = _INSPECTION_PENDING
        elif observation_phase in {Phase.SENT.value, Phase.UNCERTAIN.value} and not error_code:
            inspection = _INSPECTION_PENDING
        elif observation is None and phase in {Phase.SENT, Phase.UNCERTAIN}:
            inspection = _INSPECTION_PENDING
        else:
            inspection = _INSPECTION_UNKNOWN

        if terminal == "completed":
            completion = "completed"
        elif terminal in {"cancelled", "error"}:
            completion = "cancelled" if terminal == "cancelled" else "unknown"
        elif delivery == _DELIVERY_REJECTED:
            completion = "rejected"
        elif inspection == _INSPECTION_PENDING:
            completion = "pending"
        elif inspection == _INSPECTION_UNKNOWN:
            completion = "unknown"
        elif text:
            # Native GrokLink exposes no remote terminal marker.  Text is a
            # durable observation and remains nonterminal in Chat semantics.
            completion = "nonterminal"
        elif diag.tool_entries_observed:
            completion = "tool_only"
        elif diag.non_text_entries_observed:
            completion = "non_text"
        elif complete or diag.reply_entries_observed:
            completion = "empty"
        else:
            completion = "unknown"

        if send_attempted is None:
            send_attempted = delivery in {_DELIVERY_ACCEPTED, _DELIVERY_UNKNOWN}
            if delivery == _DELIVERY_REJECTED:
                # GrokLink uses a rejected receipt for ensure/profile failures
                # as well as an HTTP 4xx send rejection.  A send-stage
                # diagnostic means the remote call was entered.
                send_attempted = diag.stage == "send"

        correlation = _correlation(record)
        outcome: dict[str, Any] = {
            # Match the transport's protocol-success meaning: a known accepted
            # send plus a usable inspection.  A quiet-period completion still
            # remains nonterminal and is never reported as remote completion.
            "ok": delivery == _DELIVERY_ACCEPTED
            and inspection != _INSPECTION_UNKNOWN,
            "operation_id": operation_id,
            "delivery_status": delivery,
            "inspection_status": inspection,
            "completion_status": completion,
            "replies": [text] if text else [],
            "reply_observed": bool(text),
            "response_available": bool(text),
            "response_truncated": False,
            "remote_terminal": terminal or _REMOTE_TERMINAL,
            "remote_terminal_supported": terminal is not None,
            "reply_entries_observed": max(diag.reply_entries_observed, 1 if text else 0),
            "tool_entries_observed": diag.tool_entries_observed,
            "non_text_entries_observed": diag.non_text_entries_observed,
            "send_attempted": bool(send_attempted),
            "operation_completed": terminal == "completed",
            "tool_only": bool(diag.tool_entries_observed and not text),
        }
        if error_code:
            outcome["code"] = _safe_code(error_code)
        if correlation is not None:
            outcome["correlation"] = correlation

        diag_mapping = diag.as_mapping()
        diag_mapping.update(
            {
                "delivery_status": delivery,
                "inspection_status": inspection,
                "completion_status": completion,
                "reply_entries_observed": max(
                    diag.reply_entries_observed, 1 if text else 0
                ),
                "send_attempted": bool(send_attempted),
            }
        )
        # The Chat service stores only its own allow-listed projection of
        # these fields.  Keep the established Grok Bot Chat diagnostics shape and expose
        # a copy under ``diagnostics`` for direct transport callers.
        outcome.update(diag_mapping)
        outcome["diagnostics"] = {"overall": dict(diag_mapping)}
        return outcome

    def _settlement(self, operation_id: str, record: JournalRecord | None) -> str | None:
        if record is None or not SQLiteAgentLinkJournal.matches_turn(record, self._turn(operation_id)):
            return None
        if not valid_settlement(record, record.remote_ids):
            return None
        outcome = record.remote_ids.get("settlement_outcome")
        if (record.phase is Phase.REPLIED and outcome == "success") or (
            record.phase is Phase.FAILED and outcome in {"cancelled", "error"}
        ):
            return str(outcome)
        return None

    def _run_observe(self, operation_id: str) -> tuple[JournalRecord | None, Observation | None]:
        turn = self._turn(operation_id)
        result = self._runner.run(self._observe_once(turn))
        observation = result if isinstance(result, Observation) else None
        return self._record(operation_id), observation

    def execute(self, operation_id: str, prompt: str) -> dict[str, Any]:
        """Admit/send a turn once, then perform exactly one initial observe."""

        if not isinstance(operation_id, str) or not operation_id:
            return self._outcome(
                str(operation_id) if isinstance(operation_id, str) else "",
                record=None,
                error_code="OPERATION_INVALID",
                send_attempted=False,
            )
        if not isinstance(prompt, str):
            return self._outcome(
                operation_id,
                record=None,
                error_code="PROMPT_INVALID",
                send_attempted=False,
            )
        try:
            turn = self._turn(operation_id)
            receipt = self._runner.run(self._prepare_and_send(turn, prompt))
            receipt_phase = getattr(receipt, "phase", None)
            before_observe = self._record(operation_id)
            send_diag = self._last_diagnostic()
            record, observation = self._run_observe(operation_id)
            # Preserve a send failure diagnostic when the first observation is
            # a terminal journal read and the link cleared its diagnostic.
            diagnostic = self._last_diagnostic()
            if (diagnostic.code is None and send_diag.code is not None
                    and send_diag.code != LinkErrorCode.GROK_USAGE_LIMIT_EXCEEDED.value.upper()):
                diagnostic = send_diag
            return self._outcome(
                operation_id,
                record=record or before_observe,
                receipt_phase=receipt_phase,
                observation=observation,
                diagnostic=diagnostic,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            # Do not surface a fake/client exception message: it may contain
            # prompt text, a body, URL, or a credential.  If the journal row
            # exists, report its durable phase; otherwise keep the result
            # unknown and let the Chat service persist the fixed code only.
            record = None
            try:
                record = self._record(operation_id)
            except Exception:
                pass
            return self._outcome(
                operation_id,
                record=record,
                error_code="LINK_INTERNAL",
            )

    def reconcile(self, operation_id: str) -> dict[str, Any]:
        """Observe an admitted operation without ever invoking prepare/send."""

        if not isinstance(operation_id, str) or not operation_id:
            return self._outcome(
                str(operation_id) if isinstance(operation_id, str) else "",
                record=None,
                error_code="OPERATION_INVALID",
                send_attempted=False,
            )
        try:
            record = self._record(operation_id)
            if record is None:
                # Missing journal rows are reported as read-only lookup failures.
                from .grok_bot_chat_service import TransportFailure

                raise TransportFailure("ledger", "OPERATION_NOT_FOUND")
            send_diag = self._last_diagnostic()
            record, observation = self._run_observe(operation_id)
            diagnostic = self._last_diagnostic()
            if (diagnostic.code is None and send_diag.code is not None
                    and send_diag.code != LinkErrorCode.GROK_USAGE_LIMIT_EXCEEDED.value.upper()):
                diagnostic = send_diag
            return self._outcome(
                operation_id,
                record=record,
                observation=observation,
                diagnostic=diagnostic,
                send_attempted=_phase_delivery(record.phase)
                in {_DELIVERY_ACCEPTED, _DELIVERY_UNKNOWN},
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            # Preserve the Grok Bot Chat fixed-code exception
            # contract, while never serializing exception text.
            from .grok_bot_chat_service import TransportFailure

            if isinstance(error, TransportFailure):
                raise
            record = None
            try:
                record = self._record(operation_id)
            except Exception:
                pass
            return self._outcome(
                operation_id,
                record=record,
                error_code="LINK_INTERNAL",
            )

    def operation_record(self, operation_id: str) -> dict[str, Any]:
        """Return the Grok Bot Chat operation-record projection of the journal."""

        if not isinstance(operation_id, str) or not operation_id:
            from .grok_bot_chat_service import TransportFailure

            raise TransportFailure("ledger", "OPERATION_INVALID")
        try:
            record = self._record(operation_id)
        except Exception:
            record = None
        if record is None:
            from .grok_bot_chat_service import TransportFailure

            raise TransportFailure("ledger", "OPERATION_NOT_FOUND")
        turn = self._turn(operation_id)
        if not SQLiteAgentLinkJournal.matches_turn(record, turn):
            from .grok_bot_chat_service import TransportFailure

            raise TransportFailure("ledger", "CORRELATION_MISMATCH")
        delivery = _phase_delivery(record.phase)
        proof = self._settlement(operation_id, record)
        terminal = {"success": "completed", "cancelled": "cancelled", "error": "error"}.get(proof)
        correlation = _correlation(record)
        transport_state = (
            "accepted"
            if delivery == _DELIVERY_ACCEPTED
            else "rejected"
            if delivery == _DELIVERY_REJECTED
            else "uncertain"
        )
        result: dict[str, Any] = {
            "ok": True,
            "operation_id": operation_id,
            "target_identity": self._target,
            "client_nonce": turn.nonce,
            "transport_state": transport_state,
            "phase": record.phase.value,
            "native_phase": record.phase.value,
            "state": record.phase.value,
            "delivery_status": delivery,
            "inspection_status": (
                "observed" if record.phase is Phase.REPLIED else "pending"
                if record.phase in {Phase.SENT, Phase.UNCERTAIN}
                else "unknown"
            ),
            "completion_status": "nonterminal"
            if record.phase is Phase.REPLIED
            else "rejected"
            if record.phase is Phase.REJECTED
            else "unknown",
            "replies": [],
            "reply_entries_observed": 0,
            "remote_terminal": terminal or _REMOTE_TERMINAL,
            "remote_terminal_supported": terminal is not None,
            "send_attempted": delivery in {_DELIVERY_ACCEPTED, _DELIVERY_UNKNOWN},
            "operation_completed": terminal == "completed",
        }
        if record.code is not None:
            result["code"] = record.code.value
        if correlation is not None:
            result["correlation"] = correlation
            result.update(correlation)
        return result

    def acknowledge_delivery(self, operation_id: str) -> bool:
        """ACK a journal reply only after the Chat receipt was persisted."""

        if not isinstance(operation_id, str) or not operation_id:
            return False
        acknowledge = getattr(self.service, "acknowledge_delivery", None)
        if not callable(acknowledge):
            return False
        try:
            return bool(acknowledge(self._turn(operation_id)))
        except Exception:
            return False

    def close(self) -> None:
        """Close the native client on its owning loop, then stop that loop."""

        if self._closed:
            return
        self._closed = True
        link = self.link
        client = self.client
        close = getattr(link, "close", None) if link is not None else None
        if not callable(close) and client is not None:
            close = getattr(client, "close", None)
        try:
            if callable(close):
                self._runner.run(_await_if_needed(close()))
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            # Close failures must not leak native profile or transport details.
            pass
        finally:
            self._runner.close()


# Explicit aliases make the role clear at call sites while preserving the
# protocol name used by the existing Chat service.
GrokBotNativeTransport = GrokBotTransport
NativeGrokBotTransport = GrokBotTransport
NativeGrokTransport = GrokBotTransport


__all__ = [
    "GROK_RECOVERY_WINDOW_SECONDS",
    "GrokBotNativeTransport",
    "GrokBotTransport",
    "NativeGrokBotTransport",
    "NativeGrokTransport",
]
