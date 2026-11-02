"""AgentLink implementation for a locally installed Grok Bot desktop app."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
import time
from typing import Callable, Mapping

from ..codes import AgentLinkFailure, LinkErrorCode, normalize_code
from ..contracts import (
    Channel,
    LinkHealth,
    Observation,
    Phase,
    PreparedSend,
    SendReceipt,
    TurnRef,
    nonce_sha256,
)
from .client import GrokGatewayClient
from .route import GrokLinkFailure

TAIL_LIMIT = 40
THREAD_ENTRY_LIMIT = 200
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SENSITIVE_ID = re.compile(
    r"(?:token|secret|credential|password|cookie|authorization|bearer|api[_-]?key)",
    re.IGNORECASE,
)
_ID_NAMES = {
    "message_id": ("messageId", "message_id", "requestMessageId", "request_message_id"),
    "request_id": ("requestId", "request_id"),
    "root_id": ("rootId", "root_id"),
}
_CONTEXT_FIELDS = (
    ("agentId", "agent_id", "targetId", "target_id", "targetIdentity", "target_identity"),
)


def _safe_identifier(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not _IDENTIFIER.fullmatch(value)
        or _SENSITIVE_ID.search(value)
    ):
        return None
    return value


def _field(record: Mapping[str, object], *names: str) -> object:
    for name in names:
        if name in record:
            return record[name]
    return None


def _entries(body: object, *, maximum: int, stage: str) -> list[Mapping[str, object]]:
    if not isinstance(body, Mapping) or not isinstance(body.get("entries"), list):
        raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED)
    entries = body["entries"]
    if len(entries) > maximum:
        raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED)
    result: list[Mapping[str, object]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED)
        nonce = _field(entry, "clientNonce", "client_nonce")
        if nonce is not None and not isinstance(nonce, str):
            raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED)
        if stage == "thread":
            root = entry.get("rootId")
            if root is not None and not _safe_identifier(root):
                raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED)
        result.append(entry)
    return result


def _resolve_grok_bot(body: object) -> str:
    if isinstance(body, list):
        agents = body
    elif isinstance(body, Mapping) and isinstance(body.get("agents"), list):
        agents = body["agents"]
    elif isinstance(body, Mapping) and isinstance(body.get("agent"), Mapping):
        agents = [body["agent"]]
    else:
        raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED)
    matches: list[str] = []
    for agent in agents:
        if not isinstance(agent, Mapping):
            raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED)
        identifier = _safe_identifier(_field(agent, "id", "agentId", "agent_id"))
        if identifier is None:
            continue
        members = _field(agent, "memberIds", "memberAgentIds")
        is_group = agent.get("isGroup") is True or (
            agent.get("isGroup") is None and isinstance(members, list) and bool(members)
        )
        if agent.get("name") == "GrokBot" and not is_group:
            matches.append(identifier)
    if len(matches) != 1:
        raise AgentLinkFailure(LinkErrorCode.GROK_GATEWAY_REJECTED)
    return matches[0]


def _request_from_tail(
    entries: list[Mapping[str, object]],
    *,
    nonce: str,
    agent_id: str,
    baseline: Mapping[str, object],
) -> tuple[dict[str, str] | None, bool]:
    """Find exactly one nonce request and report a tail gap without exposing IDs."""
    baseline_id = _safe_identifier(baseline.get("entry_id"))
    baseline_index: int | None = None
    if baseline_id is not None:
        for index, entry in enumerate(entries):
            if entry.get("id") == baseline_id:
                baseline_index = index
                break
    gap_reset = baseline_id is not None and baseline_index is None
    matched: list[tuple[int, Mapping[str, object]]] = []
    for index, entry in enumerate(entries):
        if _field(entry, "clientNonce", "client_nonce") != nonce:
            continue
        if baseline_index is not None and index <= baseline_index:
            continue
        target = _field(entry, *_CONTEXT_FIELDS[0])
        if target is not None and target != agent_id:
            continue
        kind = entry.get("kind")
        role = entry.get("role")
        sender = entry.get("sender")
        user_message = kind == "message" and role == "user"
        legacy_user = kind == "user" and role in (None, "user")
        if not (user_message or legacy_user) or sender not in (None, "user"):
            continue
        matched.append((index, entry))
    if len(matched) != 1:
        return None, gap_reset
    _, candidate = matched[0]
    message_id = _safe_identifier(candidate.get("id"))
    request_id = _safe_identifier(_field(candidate, "requestId", "request_id"))
    root_id_value = _field(candidate, "rootId", "root_id")
    root_id = (
        _safe_identifier(root_id_value)
        if root_id_value is not None
        else message_id
    )
    if message_id is None or root_id is None:
        raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED)
    return (
        {
            "message_id": message_id,
            "request_id": request_id or "",
            "root_id": root_id,
        },
        gap_reset,
    )


def _is_tool_entry(entry: Mapping[str, object]) -> bool:
    message = entry.get("message")
    if not isinstance(message, Mapping):
        return False
    message_type = message.get("type")
    normalized = (
        re.sub(r"[-_ ]", "", message_type).lower()
        if isinstance(message_type, str)
        else ""
    )
    return normalized in {"toolcall", "toolresult", "toolstart", "toolend", "functioncall"} or any(
        key in message
        for key in (
            "toolCall",
            "tool_call",
            "toolResult",
            "tool_result",
            "functionCall",
            "function_call",
        )
    )


def _reply_evidence(
    entries: list[Mapping[str, object]],
    request: Mapping[str, str],
    *,
    nonce: str,
    agent_id: str,
) -> tuple[dict[str, Mapping[str, object]], list[str], int, int]:
    reply_entries: dict[str, Mapping[str, object]] = {}
    text_parts: list[str] = []
    tool_entries = 0
    non_text_entries = 0
    for entry in entries:
        identifier = _safe_identifier(entry.get("id"))
        if identifier is None or identifier == request["message_id"]:
            continue
        entry_nonce = _field(entry, "clientNonce", "client_nonce")
        if entry_nonce is not None and entry_nonce != nonce:
            continue
        target = _field(entry, *_CONTEXT_FIELDS[0])
        if target is not None and target != agent_id:
            continue
        request_id = _safe_identifier(_field(entry, "requestId", "request_id"))
        if not request["request_id"] or request_id != request["request_id"]:
            continue
        root_value = _field(entry, "rootId", "root_id")
        if root_value is not None and _safe_identifier(root_value) != request["root_id"]:
            continue
        if entry.get("kind") != "send-message" or entry.get("role") == "user":
            continue
        if entry.get("sender") == "user" or entry.get("kind") == "user":
            continue
        if entry.get("role") is not None and entry.get("role") not in {"assistant", "bot"}:
            continue
        if entry.get("sender") is not None and entry.get("sender") not in {"assistant", "bot"}:
            continue
        reply_entries[identifier] = entry
        message = entry.get("message")
        if (
            isinstance(message, Mapping)
            and message.get("type") == "text"
            and isinstance(message.get("content"), str)
        ):
            text_parts.append(message["content"])
        else:
            non_text_entries += 1
            if _is_tool_entry(entry):
                tool_entries += 1
    return reply_entries, text_parts, tool_entries, non_text_entries


def _receipt_remote_ids(body: object) -> dict[str, str]:
    if not isinstance(body, Mapping):
        return {}
    values: dict[str, str] = {}
    for key, names in _ID_NAMES.items():
        present = [name for name in names if name in body]
        if not present:
            continue
        rendered = [_safe_identifier(body[name]) for name in present]
        if rendered[0] is not None and all(value == rendered[0] for value in rendered):
            values[key] = rendered[0]
    return values


@dataclass(frozen=True)
class GrokDiagnostic:
    code: str
    stage: str
    http_status: int | None = None
    request_id: str | None = None
    gap_reset: bool = False
    reply_entries: int = 0
    tool_entries: int = 0
    non_text_entries: int = 0


@dataclass(frozen=True)
class GrokTerminalProof:
    target: str = field(repr=False)
    nonce_hash: str = field(repr=False)
    outcome: str
    settled_at_ms: int


class GrokLink:
    """Grok Bot AgentLink; sends once, then reconciles through transcript reads."""

    def __init__(
        self,
        client: GrokGatewayClient,
        *,
        target_agent_id: str | None = None,
        agent_resolver: Callable[[object], str] | None = None,
        quiet_period_s: float = 20.0,
        monotonic: Callable[[], float] = time.monotonic,
        next_poll_after_s: float = 2.0,
    ) -> None:
        if quiet_period_s <= 0 or next_poll_after_s <= 0:
            raise ValueError("poll timing must be positive")
        if target_agent_id is not None and _safe_identifier(target_agent_id) is None:
            raise ValueError("target agent id is invalid")
        self.client = client
        self.target_agent_id = target_agent_id
        self.agent_resolver = agent_resolver
        self.quiet_period_s = float(quiet_period_s)
        self.monotonic = monotonic
        self.next_poll_after_s = float(next_poll_after_s)
        self.last_diagnostic: GrokDiagnostic | None = None
        self.terminal_proofs: dict[str, GrokTerminalProof] = {}

    def _diagnostic(self, error: BaseException, *, stage: str | None = None) -> GrokDiagnostic:
        code = normalize_code(getattr(error, "code", LinkErrorCode.GROK_CONTRACT_CHANGED))
        inferred_stage = {
            LinkErrorCode.GROK_PROFILE_UNAVAILABLE: "profile",
            LinkErrorCode.GROK_DPAPI_FAILED: "dpapi",
            LinkErrorCode.GROK_ACCOUNT_CHANGED: "profile",
            LinkErrorCode.GROK_AUTH_EXPIRED: "ensure",
        }.get(code, "inspect")
        actual_stage = stage or getattr(error, "stage", inferred_stage)
        if actual_stage not in {"profile", "dpapi", "ensure", "gateway", "send", "inspect"}:
            actual_stage = "inspect"
        status = error.http_status if isinstance(error, GrokLinkFailure) else None
        request_id = error.request_id if isinstance(error, GrokLinkFailure) else None
        diagnostic = GrokDiagnostic(
            code=code.value,
            stage=actual_stage,
            http_status=status,
            request_id=request_id,
        )
        self.last_diagnostic = diagnostic
        return diagnostic

    def _resolve_target(self, body: object) -> str:
        if self.agent_resolver is not None:
            resolved = self.agent_resolver(body)
            target_id = _safe_identifier(resolved)
        elif self.target_agent_id is not None:
            target_id = self.target_agent_id
        else:
            target_id = _resolve_grok_bot(body)
        if target_id is None:
            raise AgentLinkFailure(LinkErrorCode.GROK_GATEWAY_REJECTED)
        return target_id

    async def prepare(self, turn: TurnRef, text: str) -> PreparedSend:
        if turn.channel is not Channel.GROK_BOT or not isinstance(text, str):
            raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL)
        try:
            target_id = self.target_agent_id
            if self.agent_resolver is not None or target_id is None:
                agents = await self.client.list_agents()
                target_id = self._resolve_target(agents)
            if target_id != turn.thread_ref:
                raise AgentLinkFailure(LinkErrorCode.GROK_GATEWAY_REJECTED)
            tail_body, _ = await self.client.get_agent_transcript_tail(
                agent_id=target_id, limit=TAIL_LIMIT
            )
            entries = _entries(tail_body, maximum=TAIL_LIMIT, stage="tail")
            entry_id = next(
                (
                    identifier
                    for entry in reversed(entries)
                    if (identifier := _safe_identifier(entry.get("id"))) is not None
                ),
                None,
            )
            baseline = {"entry_id": entry_id} if entry_id is not None else {}
            self.last_diagnostic = None
            return PreparedSend(turn=turn, text=text, baseline=baseline)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            self._diagnostic(error)
            if isinstance(error, AgentLinkFailure):
                raise
            raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL) from None

    async def send(self, prepared: PreparedSend) -> SendReceipt:
        turn = prepared.turn
        baseline = prepared.baseline
        try:
            # EnsureSandBox/profile/DPAPI failures here are provably before sendPrompt.
            await self.client.ensure_route()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            diagnostic = self._diagnostic(error, stage=getattr(error, "stage", "ensure"))
            return SendReceipt(
                turn=turn,
                phase=Phase.REJECTED,
                code=diagnostic.code,
                baseline=baseline,
            )
        try:
            response_body, response = await self.client.send_prompt(
                agent_id=turn.thread_ref,
                prompt=prepared.text,
                client_nonce=turn.nonce,
            )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            diagnostic = self._diagnostic(error, stage="send")
            code = normalize_code(getattr(error, "code", LinkErrorCode.GROK_SEND_UNCERTAIN))
            if code is LinkErrorCode.GROK_SEND_REJECTED:
                phase = Phase.REJECTED
            elif isinstance(error, GrokLinkFailure) and not error.request_started:
                phase = Phase.REJECTED
            elif isinstance(error, AgentLinkFailure) and not isinstance(error, GrokLinkFailure):
                phase = Phase.REJECTED
            else:
                phase = Phase.UNCERTAIN
                if code is LinkErrorCode.LINK_INTERNAL:
                    code = LinkErrorCode.GROK_SEND_UNCERTAIN
            return SendReceipt(
                turn=turn,
                phase=phase,
                code=code.value,
                baseline=baseline,
            )
        remote_ids = _receipt_remote_ids(response_body)
        # HTTP 2xx means accepted even when the gateway omits a message id.
        self.last_diagnostic = None
        if "message_id" not in remote_ids:
            self.last_diagnostic = GrokDiagnostic(
                code="RECEIPT_MESSAGE_ID_MISSING",
                stage="send",
                http_status=response.status,
                request_id=response.request_id,
            )
        return SendReceipt(
            turn=turn,
            phase=Phase.SENT,
            remote_ids=remote_ids,
            baseline=baseline,
        )

    async def observe(
        self, turn: TurnRef, baseline: Mapping[str, object]
    ) -> Observation:
        self.terminal_proofs.pop(turn.turn_id, None)
        try:
            tail_body, _ = await self.client.get_agent_transcript_tail(
                agent_id=turn.thread_ref, limit=TAIL_LIMIT
            )
            tail_entries = _entries(tail_body, maximum=TAIL_LIMIT, stage="tail")
            request, gap_reset = _request_from_tail(
                tail_entries,
                nonce=turn.nonce,
                agent_id=turn.thread_ref,
                baseline=baseline,
            )
            # Status is independently correlated by target and native nonce.  A
            # missing tail request prevents attaching any text, but must not hide
            # an authoritative cancelled/error settlement.
            evidence: dict[str, Mapping[str, object]] = {}
            text_parts: list[str] = []
            tool_count = non_text_count = 0
            thread_error: BaseException | None = None
            if request is not None and request["request_id"]:
                evidence, text_parts, tool_count, non_text_count = _reply_evidence(
                    tail_entries,
                    request,
                    nonce=turn.nonce,
                    agent_id=turn.thread_ref,
                )
                if not text_parts:
                    try:
                        thread_body, _ = await self.client.get_agent_thread(
                            agent_id=turn.thread_ref,
                            root_id=request["root_id"],
                        )
                        if not isinstance(thread_body, Mapping):
                            raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED)
                        body_root = thread_body.get("rootId")
                        if body_root is not None and body_root != request["root_id"]:
                            raise AgentLinkFailure(LinkErrorCode.GROK_CONTRACT_CHANGED)
                        thread_entries = _entries(
                            thread_body, maximum=THREAD_ENTRY_LIMIT, stage="thread"
                        )
                        thread_entries = [
                            entry
                            for entry in thread_entries
                            if entry.get("rootId") is None
                            or entry.get("rootId") == request["root_id"]
                        ]
                        combined: dict[str, Mapping[str, object]] = {
                            entry_id: entry for entry_id, entry in evidence.items()
                        }
                        thread_evidence, thread_text, thread_tools, thread_non_text = _reply_evidence(
                            thread_entries,
                            request,
                            nonce=turn.nonce,
                            agent_id=turn.thread_ref,
                        )
                        for entry_id, entry in thread_evidence.items():
                            combined.setdefault(entry_id, entry)
                        evidence = combined
                        text_parts = []
                        for entry_id in combined:
                            message = combined[entry_id].get("message")
                            if (
                                isinstance(message, Mapping)
                                and message.get("type") == "text"
                                and isinstance(message.get("content"), str)
                            ):
                                text_parts.append(message["content"])
                        tool_count = max(tool_count, thread_tools)
                        non_text_count = max(non_text_count, thread_non_text)
                    except BaseException as error:
                        if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                            raise
                        thread_error = error

            reply_text = "\n".join(text_parts) if text_parts else None
            try:
                status_items = await self.client.list_agent_statuses([turn.thread_ref])
            except AgentLinkFailure as error:
                diagnostic = self._diagnostic(error, stage="inspect")
                return Observation(
                    phase=Phase.SENT, reply_text=reply_text, complete=False,
                    code=diagnostic.code, next_poll_after_s=self.next_poll_after_s,
                )
            status = status_items[0] if len(status_items) == 1 else None
            if status is None or status.live is None:
                fallback = getattr(self.client, "get_selected_live_status", None)
                if callable(fallback):
                    try:
                        fallback_status = await fallback(turn.thread_ref)
                        if fallback_status is not None and status is not None:
                            from dataclasses import replace
                            merged_raw = dict(status.raw)
                            merged_raw["live"] = fallback_status.raw["live"]
                            status = replace(status, live=fallback_status.live, raw=merged_raw)
                        else:
                            status = fallback_status
                    except AgentLinkFailure as error:
                        diagnostic = self._diagnostic(error, stage="inspect")
                        return Observation(
                            phase=Phase.SENT, reply_text=reply_text, complete=False,
                            code=diagnostic.code, next_poll_after_s=self.next_poll_after_s,
                        )
            if status is not None and status.live is not None:
                live = status.raw.get("live")
                settlement = status.live.last_turn_settlement
                # The installed status schema has no child-agent ID field.
                # An unexpected such field cannot establish an idle subtree.
                unknown_children = any(
                    key in container
                    for container in (status.raw, live)
                    for key in ("childIds", "childAgentIds", "subagentIds")
                )
                running = unknown_children or status.is_running or any(
                    live.get(name, False)
                    for name in (
                        "isRunning", "isComposingMessage", "isRetrying",
                        "hasRunningSubagents",
                    )
                )
                if (
                    status.agent_id == turn.thread_ref
                    and (self.target_agent_id is None or self.target_agent_id == turn.thread_ref)
                    and settlement is not None
                    and settlement.turn_id == turn.nonce
                    and settlement.outcome in {"success", "cancelled", "error"}
                    and settlement.settled_at_ms > 0
                    and not running
                    and (settlement.outcome != "success" or reply_text is not None)
                ):
                    self.terminal_proofs[turn.turn_id] = GrokTerminalProof(
                        turn.thread_ref, nonce_sha256(turn.nonce),
                        settlement.outcome, settlement.settled_at_ms,
                    )
                    self.last_diagnostic = None
                    return Observation(
                        phase=Phase.REPLIED if settlement.outcome == "success" else Phase.FAILED,
                        reply_text=reply_text,
                        complete=True,
                        complete_basis="provider_terminal",
                        remote_ids={
                            "settlement_hash": nonce_sha256(turn.nonce),
                            "settlement_target": turn.thread_ref,
                            "settlement_outcome": settlement.outcome,
                            "settled_at_ms": settlement.settled_at_ms,
                        },
                        code=None if settlement.outcome == "success" else LinkErrorCode.LINK_INTERNAL.value,
                    )
            availability = None
            if reply_text is None and thread_error is None:
                try:
                    availability = await self.client.get_sand_usage_availability()
                except AgentLinkFailure as error:
                    if error.code is LinkErrorCode.GROK_ACCOUNT_CHANGED:
                        raise
            if availability is False:
                self.last_diagnostic = GrokDiagnostic(
                    code=LinkErrorCode.GROK_USAGE_LIMIT_EXCEEDED.value, stage="inspect"
                )
                return Observation(
                    phase=Phase.SENT, complete=False, code=self.last_diagnostic.code,
                    next_poll_after_s=self.next_poll_after_s,
                )
            self.last_diagnostic = (
                self._diagnostic(thread_error, stage="inspect")
                if thread_error is not None
                else GrokDiagnostic(
                    code=LinkErrorCode.GROK_REPLY_PENDING.value,
                    stage="inspect", gap_reset=gap_reset, reply_entries=len(evidence),
                    tool_entries=tool_count, non_text_entries=non_text_count,
                )
            )
            return Observation(
                phase=Phase.SENT, reply_text=reply_text, complete=False,
                code=self.last_diagnostic.code, next_poll_after_s=self.next_poll_after_s,
            )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            diagnostic = self._diagnostic(error, stage="inspect")
            return Observation(
                phase=Phase.SENT,
                complete=False,
                code=diagnostic.code,
                next_poll_after_s=self.next_poll_after_s,
            )

    async def health(self) -> LinkHealth:
        started = self.monotonic()
        try:
            await self.client.ensure_route()
            self.last_diagnostic = None
            return LinkHealth(
                healthy=True,
                stage="health",
                elapsed_ms=max(0, int((self.monotonic() - started) * 1000)),
            )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            diagnostic = self._diagnostic(error)
            return LinkHealth(
                healthy=False,
                code=diagnostic.code,
                stage=(
                    diagnostic.stage
                    if diagnostic.stage in {"prepare", "send", "observe", "health", "internal"}
                    else "health"
                ),
                http_status=diagnostic.http_status,
                elapsed_ms=max(0, int((self.monotonic() - started) * 1000)),
            )

    async def close(self) -> None:
        await self.client.close()
