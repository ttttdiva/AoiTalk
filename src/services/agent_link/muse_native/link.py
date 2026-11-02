"""MuseLink: AgentLink implementation backed by the native Muse gateway."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import time
from collections.abc import Mapping
from pathlib import Path

from ..codes import AgentLinkFailure, LinkErrorCode, normalize_code
from ..contracts import (
    Channel,
    LinkHealth,
    Observation,
    Phase,
    PreparedSend,
    SendReceipt,
    TurnRef,
)
from .auth import AuthClient
from .gateway import GatewayClient, GatewayFailure

_NODE_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_POLL_DELAYS = (2.0, 4.0, 15.0)


def load_or_create_node_id(path: Path) -> str:
    """Load or atomically create a non-secret 16-hex device identifier."""

    try:
        existing = path.read_text(encoding="ascii").strip()
        if _NODE_ID_RE.fullmatch(existing):
            return existing
        raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
    except FileNotFoundError:
        value = secrets.token_hex(8)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="ascii") as handle:
                handle.write(value + "\n")
            return value
        except FileExistsError:
            try:
                existing = path.read_text(encoding="ascii").strip()
            except (OSError, UnicodeError):
                raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED) from None
            if _NODE_ID_RE.fullmatch(existing):
                return existing
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED) from None
        except (OSError, UnicodeError):
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED) from None
    except AgentLinkFailure:
        raise
    except (OSError, UnicodeError):
        raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED) from None


class MuseLink:
    def __init__(
        self,
        auth: AuthClient,
        gateway: GatewayClient,
        *,
        node_id: str | None = None,
        node_id_path: Path | None = None,
        send_attempts: int = 3,
        retry_delays_s: tuple[float, ...] = (0.25, 0.5),
        stream_ack_read_s: float = 2.0,
        sleep=asyncio.sleep,
        monotonic=time.monotonic,
    ) -> None:
        if node_id is not None and node_id_path is not None:
            raise ValueError("provide node_id or node_id_path")
        if node_id is None:
            if node_id_path is None:
                raise ValueError("provide a stable node_id or node_id_path")
            node_id = load_or_create_node_id(node_id_path)
        if not isinstance(node_id, str) or not _NODE_ID_RE.fullmatch(node_id):
            raise ValueError("node identifier is invalid")
        if not 1 <= send_attempts <= 3 or len(retry_delays_s) < send_attempts - 1:
            raise ValueError("send retry policy is invalid")
        if any(delay < 0 for delay in retry_delays_s):
            raise ValueError("send retry policy is invalid")
        try:
            stream_ack_read_s = float(stream_ack_read_s)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("stream acknowledgement window is invalid") from None
        if not math.isfinite(stream_ack_read_s) or stream_ack_read_s < 0:
            raise ValueError("stream acknowledgement window is invalid")
        self.auth = auth
        self.gateway = gateway
        self._node_id = node_id
        self.send_attempts = send_attempts
        self.retry_delays_s = retry_delays_s
        self.stream_ack_read_s = stream_ack_read_s
        self._sleep = sleep
        self._monotonic = monotonic
        self._poll_count: dict[str, int] = {}

    def __repr__(self) -> str:
        return "MuseLink(<credentials and device state redacted>)"

    async def _ready(self) -> None:
        session = await self.auth.authenticate()
        await self.gateway.connect(session.vm_id, session.hatch_token)

    async def _call(self, method: str, *, body=None, query=None, timeout=30.0) -> object:
        await self._ready()
        return await self.gateway.request(
            method,
            path_params=None,
            body=body,
            query=query,
            timeout=timeout,
        )

    @staticmethod
    def _object(value: object) -> Mapping[str, object]:
        if isinstance(value, Mapping):
            return value
        if isinstance(value, bytes):
            try:
                decoded = json.loads(value.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED) from None
            if isinstance(decoded, Mapping):
                return decoded
        raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)

    @staticmethod
    def _history_events(response: object) -> list[Mapping[str, object]]:
        data = MuseLink._object(response)
        if "chat_events" not in data and isinstance(data.get("result"), Mapping):
            # The daemon wraps JSON as {"ok": true, "result": {...}}.
            data = data["result"]
        events = data.get("chat_events")
        if not isinstance(events, list):
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
        normalized: list[Mapping[str, object]] = []
        for event in events:
            if isinstance(event, Mapping):
                normalized.append(event)
        return normalized

    @staticmethod
    def _seq(event: Mapping[str, object]) -> int | None:
        value = event.get("seq")
        if type(value) is int:
            return value
        if isinstance(value, str) and value.isdecimal():
            try:
                return int(value)
            except ValueError:
                return None
        return None

    async def prepare(self, turn: TurnRef, text: str) -> PreparedSend:
        if turn.channel is not Channel.MUSE or not isinstance(text, str):
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
        response = await self._call(
            "chat.history",
            query={"limit": 1, "session_id": turn.thread_ref},
        )
        events = self._history_events(response)
        seq_values = [seq for event in events if (seq := self._seq(event)) is not None]
        baseline_seq = max(seq_values, default=0)
        return PreparedSend(
            turn=turn,
            text=text,
            baseline={
                "seq": baseline_seq,
                "body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            },
        )

    async def send(self, prepared: PreparedSend) -> SendReceipt:
        if prepared.turn.channel is not Channel.MUSE:
            return SendReceipt(
                turn=prepared.turn,
                phase=Phase.REJECTED,
                code=LinkErrorCode.MUSE_CONTRACT_CHANGED.value,
                baseline=prepared.baseline,
            )
        body = {
            "items": [{"type": "text", "text": prepared.text}],
            "node_id": self._node_id,
            "capabilities": {},
            "session_id": prepared.turn.thread_ref,
        }
        last_code = LinkErrorCode.MUSE_CONNECT_FAILED
        for attempt in range(self.send_attempts):
            try:
                await self._ready()
            except AgentLinkFailure as error:
                last_code = normalize_code(error.code)
            except Exception:
                last_code = LinkErrorCode.MUSE_CONNECT_FAILED
            else:
                try:
                    await self.gateway.send_subscription(
                        "chat.stream",
                        path_params=None,
                        body=body,
                        query={"noiseOnly": "true", "streamMode": "subscription"},
                        timeout=self.stream_ack_read_s,
                    )
                    return SendReceipt(
                        turn=prepared.turn,
                        phase=Phase.SENT,
                        baseline=prepared.baseline,
                    )
                except GatewayFailure as error:
                    last_code = normalize_code(error.code)
                    if error.may_have_been_sent:
                        if error.http_status is not None and 400 <= error.http_status < 500:
                            return SendReceipt(
                                turn=prepared.turn,
                                phase=Phase.REJECTED,
                                code=LinkErrorCode.MUSE_GATEWAY_STATUS.value,
                                baseline=prepared.baseline,
                            )
                        return SendReceipt(
                            turn=prepared.turn,
                            phase=Phase.UNCERTAIN,
                            code=LinkErrorCode.MUSE_SEND_UNCERTAIN.value,
                            baseline=prepared.baseline,
                        )
                except AgentLinkFailure as error:
                    # Once the subscription request is entered without a typed
                    # pre-write marker, the remote may already have accepted it.
                    return SendReceipt(
                        turn=prepared.turn,
                        phase=Phase.UNCERTAIN,
                        code=LinkErrorCode.MUSE_SEND_UNCERTAIN.value,
                        baseline=prepared.baseline,
                    )
                except Exception:
                    return SendReceipt(
                        turn=prepared.turn,
                        phase=Phase.UNCERTAIN,
                        code=LinkErrorCode.MUSE_SEND_UNCERTAIN.value,
                        baseline=prepared.baseline,
                    )
            if attempt + 1 < self.send_attempts:
                await self._sleep(self.retry_delays_s[attempt])
        return SendReceipt(
            turn=prepared.turn,
            phase=Phase.REJECTED,
            code=last_code.value,
            baseline=prepared.baseline,
        )

    @staticmethod
    def _event_payload(event: Mapping[str, object]) -> Mapping[str, object]:
        value = event.get("payload")
        return value if isinstance(value, Mapping) else {}

    async def observe(
        self, turn: TurnRef, baseline: Mapping[str, object]
    ) -> Observation:
        poll_index = self._poll_count.get(turn.turn_id, 0)
        delay = _POLL_DELAYS[min(poll_index, len(_POLL_DELAYS) - 1)]
        self._poll_count[turn.turn_id] = poll_index + 1
        if turn.channel is not Channel.MUSE:
            return Observation(
                phase=Phase.SENT,
                code=LinkErrorCode.MUSE_CONTRACT_CHANGED.value,
                next_poll_after_s=delay,
            )
        try:
            baseline_seq = baseline.get("seq")
            body_digest = baseline.get("body_sha256")
            if type(baseline_seq) is not int or not isinstance(body_digest, str):
                raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
            history = await self._call(
                "chat.history",
                query={"limit": 1000, "session_id": turn.thread_ref},
                timeout=30.0,
            )
            events = self._history_events(history)
            matching_users: list[tuple[int, Mapping[str, object]]] = []
            for event in events:
                seq = self._seq(event)
                payload = self._event_payload(event)
                event_name = event.get("event_name", event.get("type"))
                body = payload.get("display_text", event.get("display_text"))
                if (
                    seq is not None
                    and seq > baseline_seq
                    and event_name == "message.user"
                    and isinstance(body, str)
                    and hashlib.sha256(body.encode("utf-8")).hexdigest() == body_digest
                ):
                    matching_users.append((seq, event))
            if not matching_users:
                return Observation(phase=Phase.SENT, next_poll_after_s=delay)

            user_seq = min(item[0] for item in matching_users)
            replies: list[tuple[int, str, object]] = []
            for event in events:
                seq = self._seq(event)
                if seq is None or seq <= user_seq:
                    continue
                if event.get("event_name", event.get("type")) != "message.assistant":
                    continue
                payload = self._event_payload(event)
                reply_to = event.get("reply_to_message_id")
                if reply_to is None:
                    reply_to = payload.get("reply_to_message_id")
                if reply_to not in (None, ""):
                    continue
                body = payload.get("display_text", event.get("display_text"))
                if not isinstance(body, str) or not body:
                    continue
                ready = payload.get(
                    "display_text_ready", event.get("display_text_ready")
                )
                if ready is False:
                    continue
                replies.append((seq, body, ready))
            if not replies:
                return Observation(phase=Phase.SENT, next_poll_after_s=delay)
            _seq_value, reply_text, ready_value = max(replies, key=lambda item: item[0])
            complete = ready_value is True
            return Observation(
                phase=Phase.REPLIED,
                reply_text=reply_text,
                complete=complete,
                complete_basis="display_text_ready" if complete else None,
                next_poll_after_s=delay,
            )
        except AgentLinkFailure as error:
            return Observation(
                phase=Phase.SENT,
                code=normalize_code(error.code).value,
                next_poll_after_s=delay,
            )
        except Exception:
            return Observation(
                phase=Phase.SENT,
                code=LinkErrorCode.LINK_INTERNAL.value,
                next_poll_after_s=delay,
            )

    async def create_thread(self, title: str | None = None) -> str:
        if title is not None and not isinstance(title, str):
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
        params: dict[str, object] = {"origin": "fresh", "lifecycle": "persistent"}
        if title:
            params["title"] = title
        result = await self._call(
            "session.start",
            body={"method": "/api/session/start", "params": params},
        )
        data = self._object(result)
        candidate = data.get("session_id")
        if candidate is None and isinstance(data.get("result"), Mapping):
            candidate = data["result"].get("session_id")
        if not isinstance(candidate, str) or not _SESSION_ID_RE.fullmatch(candidate):
            raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
        return candidate

    async def sessions_list(self) -> object:
        return await self._call("sessions.list")

    async def health(self) -> LinkHealth:
        started = self._monotonic()
        try:
            await self._call("vm.health", timeout=20.0)
            return LinkHealth(
                healthy=True,
                stage="health",
                elapsed_ms=int(max(0.0, self._monotonic() - started) * 1000),
            )
        except GatewayFailure as error:
            return LinkHealth(
                healthy=False,
                code=normalize_code(error.code).value,
                stage="health",
                http_status=error.http_status,
                elapsed_ms=int(max(0.0, self._monotonic() - started) * 1000),
            )
        except AgentLinkFailure as error:
            return LinkHealth(
                healthy=False,
                code=normalize_code(error.code).value,
                stage="health",
                elapsed_ms=int(max(0.0, self._monotonic() - started) * 1000),
            )
        except Exception:
            return LinkHealth(
                healthy=False,
                code=LinkErrorCode.LINK_INTERNAL.value,
                stage="health",
                elapsed_ms=int(max(0.0, self._monotonic() - started) * 1000),
            )

    async def close(self) -> None:
        try:
            await self.gateway.close()
        finally:
            await self.auth.close()
