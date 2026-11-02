"""Async multiplexed websocket gateway with one stateful Noise reader task."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import ssl
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any, Callable
from urllib.parse import urlencode

from websockets.asyncio.client import connect as websocket_connect

from ..codes import AgentLinkFailure, LinkErrorCode, normalize_code
from .noise_xx import CipherState, NoiseXXInitiator
from .proto import (
    ApplicationRequest,
    ApplicationResponse,
    BodyChunk,
    FrameAssembler,
    NoiseTransportFrame,
    Reset,
    ServiceFrame,
    ServiceRequest,
    ServiceResponse,
    make_outer_frame,
    parse,
    serialize,
)
from .routes import resolve_request


class GatewayFailure(AgentLinkFailure):
    """Safe gateway error carrying only a fixed code, status, and send marker."""

    def __init__(
        self,
        code: object,
        *,
        may_have_been_sent: bool = False,
        http_status: int | None = None,
    ) -> None:
        super().__init__(code)
        self.may_have_been_sent = bool(may_have_been_sent)
        self.http_status = (
            http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        )

    def __repr__(self) -> str:
        return (
            f"GatewayFailure(code={self.code.value!r}, "
            f"may_have_been_sent={self.may_have_been_sent}, "
            f"http_status={self.http_status!r})"
        )


_LOG = logging.getLogger("aoi.muse.native.gateway")
_MAX_RESPONDER_PAYLOAD = 4096


class GatewayClient:
    def __init__(
        self,
        *,
        connector: Callable[..., Any] | None = None,
        handshake_timeout: float = 20.0,
        max_pending: int = 256,
    ) -> None:
        self._connector = connector or websocket_connect
        self.handshake_timeout = handshake_timeout
        self.max_pending = max_pending
        self._websocket_logger = logging.Logger(
            "aoi.muse.native.websocket", level=logging.CRITICAL + 1
        )
        self._websocket_logger.disabled = True
        self._websocket_logger.propagate = False
        self._connect_lock = asyncio.Lock()
        self._websocket: Any | None = None
        self._initiator: NoiseXXInitiator | None = None
        self._send_cipher: CipherState | None = None
        self._receive_cipher: CipherState | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Queue[ServiceFrame | GatewayFailure]] = {}
        self._next_stream_id = 1
        self._send_lock = asyncio.Lock()
        self._assembler = FrameAssembler()

    def __repr__(self) -> str:
        return "GatewayClient(<connection credentials redacted>)"

    @property
    def connected(self) -> bool:
        return (
            self._websocket is not None
            and self._reader_task is not None
            and not self._reader_task.done()
            and self._send_cipher is not None
            and self._receive_cipher is not None
        )

    async def connect(self, vm_id: str, hatch_token: str) -> None:
        async with self._connect_lock:
            if self.connected:
                return
            await self._close_unlocked()
            if (
                not isinstance(vm_id, str)
                or not vm_id
                or not isinstance(hatch_token, str)
                or not hatch_token
            ):
                raise GatewayFailure(LinkErrorCode.MUSE_CONNECT_FAILED)
            url = "wss://hatch.metaaivm.com/v1/noise?" + urlencode(
                {"vm_id": vm_id, "auth_token": hatch_token}
            )
            websocket = None
            initiator = NoiseXXInitiator()
            try:
                websocket = await self._connector(
                    url,
                    ssl=ssl.create_default_context(),
                    open_timeout=self.handshake_timeout,
                    close_timeout=2.0,
                    max_size=4 * 1024 * 1024,
                    logger=self._websocket_logger,
                )
            except Exception:
                raise GatewayFailure(LinkErrorCode.MUSE_CONNECT_FAILED) from None
            try:
                await websocket.send(initiator.start())
                response = await asyncio.wait_for(
                    websocket.recv(), timeout=self.handshake_timeout
                )
                if not isinstance(response, (bytes, bytearray)):
                    # Only the frame kind and size are recorded; the content
                    # of a non-Noise frame is never logged.
                    _LOG.warning(
                        "Muse handshake unsupported: reason=non_binary_frame length=%d",
                        len(response) if hasattr(response, "__len__") else -1,
                    )
                    raise GatewayFailure(LinkErrorCode.MUSE_HANDSHAKE_UNSUPPORTED)
                responder_payload = initiator.receive_message2(bytes(response))
                if len(responder_payload) > _MAX_RESPONDER_PAYLOAD:
                    _LOG.warning(
                        "Muse handshake unsupported: reason=responder_payload_too_large length=%d",
                        len(responder_payload),
                    )
                    raise GatewayFailure(LinkErrorCode.MUSE_HANDSHAKE_UNSUPPORTED)
                # The standard-mode responder may carry an informational,
                # already-authenticated payload in message 2.  Only its size is
                # recorded; its content is not used or logged.
                if responder_payload:
                    _LOG.info(
                        "Muse handshake responder payload ignored: length=%d",
                        len(responder_payload),
                    )
                await websocket.send(initiator.finish())
                if initiator.send_cipher is None or initiator.receive_cipher is None:
                    raise GatewayFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED)
            except asyncio.CancelledError:
                try:
                    await websocket.close()
                except Exception:
                    pass
                raise
            except GatewayFailure:
                try:
                    await websocket.close()
                except Exception:
                    pass
                raise
            except Exception:
                try:
                    await websocket.close()
                except Exception:
                    pass
                raise GatewayFailure(LinkErrorCode.MUSE_HANDSHAKE_FAILED) from None

            self._websocket = websocket
            self._initiator = initiator
            self._send_cipher = initiator.send_cipher
            self._receive_cipher = initiator.receive_cipher
            self._assembler = FrameAssembler()
            self._reader_task = asyncio.create_task(self._receive_loop())

    async def _drop(self, code: LinkErrorCode) -> None:
        async with self._connect_lock:
            await self._drop_unlocked(code)

    async def _drop_unlocked(self, code: LinkErrorCode) -> None:
        websocket = self._websocket
        self._websocket = None
        self._send_cipher = None
        self._receive_cipher = None
        self._initiator = None
        task = self._reader_task
        self._reader_task = None
        for queue in tuple(self._pending.values()):
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                queue.put_nowait(
                    GatewayFailure(code, may_have_been_sent=True)
                )
            except asyncio.QueueFull:
                pass
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                pass

    async def _receive_loop(self) -> None:
        websocket = self._websocket
        cipher = self._receive_cipher
        if websocket is None or cipher is None:
            return
        try:
            while True:
                raw = await websocket.recv()
                if not isinstance(raw, (bytes, bytearray)):
                    raise AgentLinkFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT)
                # Wire order (muse-cli gateway.py, PROTOCOL.md): the binary WS
                # message is the Noise ciphertext of a serialized
                # NoiseTransportFrame; the frame's payload is the ServiceResponse.
                outer = parse(NoiseTransportFrame, cipher.decrypt(bytes(raw)))
                plaintext = self._assembler.add(outer)
                if plaintext is None:
                    continue
                service_response = parse(ServiceResponse, plaintext)
                frame = parse(ServiceFrame, bytes(service_response.payload))
                kind = frame.WhichOneof("kind")
                if kind not in {"response", "body_chunk", "reset"}:
                    raise AgentLinkFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT)
                queue = self._pending.get(int(frame.stream_id))
                if queue is None:
                    continue
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    raise AgentLinkFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT)
        except asyncio.CancelledError:
            raise
        except BaseException:
            await self._drop(LinkErrorCode.MUSE_TRANSPORT_CORRUPT)

    def _build_request(
        self,
        method: str,
        path_params: Mapping[str, object] | None,
        body: object,
        query: Mapping[str, object] | None,
    ) -> ServiceFrame:
        route, path = resolve_request(method, path_params, query)
        request = ApplicationRequest()
        request.verb = route.verb
        request.path = path
        request.headers.add(key="x-request-id", value=str(uuid.uuid4()))
        encoded_body = b""
        if body is not None:
            if isinstance(body, bytes):
                encoded_body = body
            else:
                try:
                    encoded_body = json.dumps(
                        body, separators=(",", ":"), ensure_ascii=False
                    ).encode("utf-8")
                except (TypeError, ValueError):
                    raise AgentLinkFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED) from None
            request.headers.add(key="content-type", value="application/json")
            request.body = encoded_body
        request.end_body = True
        frame = ServiceFrame()
        frame.stream_id = self._next_stream_id
        frame.request.CopyFrom(request)
        return frame

    async def _send_request(
        self,
        method: str,
        *,
        path_params: Mapping[str, object] | None = None,
        body: object = None,
        query: Mapping[str, object] | None = None,
    ) -> tuple[int, asyncio.Queue[ServiceFrame | GatewayFailure]]:
        if not self.connected:
            raise GatewayFailure(LinkErrorCode.MUSE_CONNECT_FAILED)
        if len(self._pending) >= self.max_pending:
            raise GatewayFailure(LinkErrorCode.MUSE_GATEWAY_STATUS)
        stream_id = self._next_stream_id
        try:
            frame = self._build_request(method, path_params, body, query)
            service_request = ServiceRequest()
            service_request.service = 0
            service_request.payload = serialize(frame)
            plaintext = serialize(service_request)
        except AgentLinkFailure as error:
            raise GatewayFailure(normalize_code(error.code)) from None
        except Exception:
            raise GatewayFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED) from None
        self._next_stream_id = stream_id + 1
        queue: asyncio.Queue[ServiceFrame | GatewayFailure] = asyncio.Queue(maxsize=256)
        self._pending[stream_id] = queue
        try:
            async with self._send_lock:
                if not self.connected or self._websocket is None or self._send_cipher is None:
                    raise GatewayFailure(LinkErrorCode.MUSE_CONNECT_FAILED)
                try:
                    outer = make_outer_frame(
                        plaintext, chunk_id=secrets.randbits(63)
                    )
                    encoded_outer = self._send_cipher.encrypt(serialize(outer))
                except AgentLinkFailure as error:
                    raise GatewayFailure(normalize_code(error.code)) from None
                try:
                    await self._websocket.send(encoded_outer)
                except Exception:
                    await self._drop(LinkErrorCode.MUSE_CONNECT_FAILED)
                    raise GatewayFailure(
                        LinkErrorCode.MUSE_TRANSPORT_CORRUPT,
                        may_have_been_sent=True,
                    ) from None
            return stream_id, queue
        except BaseException:
            self._pending.pop(stream_id, None)
            raise

    async def _next_frame(
        self,
        queue: asyncio.Queue[ServiceFrame | GatewayFailure],
        deadline: float,
    ) -> ServiceFrame:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise GatewayFailure(LinkErrorCode.MUSE_GATEWAY_STATUS, may_have_been_sent=True)
        try:
            event = await asyncio.wait_for(queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            raise GatewayFailure(LinkErrorCode.MUSE_GATEWAY_STATUS, may_have_been_sent=True) from None
        if isinstance(event, GatewayFailure):
            raise event
        if event.WhichOneof("kind") == "reset":
            raise GatewayFailure(LinkErrorCode.MUSE_GATEWAY_STATUS, may_have_been_sent=True)
        return event

    async def request(
        self,
        method: str,
        path_params: Mapping[str, object] | None = None,
        body: object = None,
        query: Mapping[str, object] | None = None,
        timeout: float = 30.0,
    ) -> object:
        stream_id, queue = await self._send_request(
            method, path_params=path_params, body=body, query=query
        )
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            first = await self._next_frame(queue, deadline)
            if first.WhichOneof("kind") != "response":
                raise GatewayFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT, may_have_been_sent=True)
            response: ApplicationResponse = first.response
            status = int(response.status)
            if not 100 <= status <= 599:
                raise GatewayFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED, may_have_been_sent=True)
            headers = {item.key.lower(): item.value for item in response.headers}
            content = bytearray(response.body)
            finished = bool(response.end_body)
            while not finished:
                next_frame = await self._next_frame(queue, deadline)
                if next_frame.WhichOneof("kind") != "body_chunk":
                    raise GatewayFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT, may_have_been_sent=True)
                chunk: BodyChunk = next_frame.body_chunk
                content.extend(chunk.data)
                finished = bool(chunk.end_body)
                if len(content) > 16 * 1024 * 1024:
                    raise GatewayFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT, may_have_been_sent=True)
            if status >= 400:
                raise GatewayFailure(
                    LinkErrorCode.MUSE_GATEWAY_STATUS,
                    may_have_been_sent=True,
                    http_status=status,
                )
            return self._decode_response(bytes(content), headers)
        except GatewayFailure as failure:
            if failure.may_have_been_sent:
                raise
            raise GatewayFailure(
                failure.code,
                may_have_been_sent=True,
                http_status=failure.http_status,
            ) from None
        except AgentLinkFailure as failure:
            raise GatewayFailure(
                normalize_code(failure.code), may_have_been_sent=True
            ) from None
        except Exception:
            raise GatewayFailure(
                LinkErrorCode.MUSE_TRANSPORT_CORRUPT, may_have_been_sent=True
            ) from None
        finally:
            self._pending.pop(stream_id, None)

    async def send_subscription(
        self,
        method: str,
        path_params: Mapping[str, object] | None = None,
        body: object = None,
        query: Mapping[str, object] | None = None,
        timeout: float = 2.0,
    ) -> None:
        """Write a subscription request, then briefly drain its optional echo.

        A successful websocket write is the acceptance boundary for
        ``chat.stream``. Response frames are best-effort: timeout, reset, or
        disconnect after the write must not turn an accepted send into a retry.
        """

        try:
            read_window = float(timeout)
        except (TypeError, ValueError, OverflowError):
            raise GatewayFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED) from None
        if not math.isfinite(read_window) or read_window < 0:
            raise GatewayFailure(LinkErrorCode.MUSE_CONTRACT_CHANGED)
        stream_id, queue = await self._send_request(
            method, path_params=path_params, body=body, query=query
        )
        deadline = asyncio.get_running_loop().time() + read_window
        body_finished = False
        saw_response = False
        try:
            while asyncio.get_running_loop().time() < deadline and not body_finished:
                try:
                    frame = await self._next_frame(queue, deadline)
                except GatewayFailure:
                    # The request is already written. Its echo is not evidence
                    # required to establish SENT and isn't safe to retry.
                    return
                kind = frame.WhichOneof("kind")
                if kind == "response" and not saw_response:
                    saw_response = True
                    response: ApplicationResponse = frame.response
                    body_finished = bool(response.end_body)
                elif kind == "body_chunk" and saw_response:
                    chunk: BodyChunk = frame.body_chunk
                    body_finished = bool(chunk.end_body)
                else:
                    return
        except Exception:
            # A malformed or interrupted echo after write is also best-effort.
            return
        finally:
            self._pending.pop(stream_id, None)

    @staticmethod
    def _decode_response(content: bytes, headers: Mapping[str, str]) -> object:
        content_type = headers.get("content-type", "").lower()
        if "json" not in content_type:
            return content
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GatewayFailure(
                LinkErrorCode.MUSE_CONTRACT_CHANGED, may_have_been_sent=True
            ) from None

    async def subscribe(
        self,
        method: str,
        path_params: Mapping[str, object] | None = None,
        body: object = None,
        query: Mapping[str, object] | None = None,
        timeout: float = 300.0,
    ) -> AsyncIterator[bytes]:
        stream_id, queue = await self._send_request(
            method, path_params=path_params, body=body, query=query
        )
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            first = await self._next_frame(queue, deadline)
            if first.WhichOneof("kind") != "response":
                raise GatewayFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT, may_have_been_sent=True)
            response: ApplicationResponse = first.response
            if int(response.status) >= 400:
                raise GatewayFailure(
                    LinkErrorCode.MUSE_GATEWAY_STATUS,
                    may_have_been_sent=True,
                    http_status=int(response.status),
                )
            if response.body:
                yield bytes(response.body)
            if response.end_body:
                return
            while True:
                frame = await self._next_frame(queue, deadline)
                if frame.WhichOneof("kind") != "body_chunk":
                    raise GatewayFailure(LinkErrorCode.MUSE_TRANSPORT_CORRUPT, may_have_been_sent=True)
                chunk: BodyChunk = frame.body_chunk
                if chunk.data:
                    yield bytes(chunk.data)
                if chunk.end_body:
                    return
        finally:
            self._pending.pop(stream_id, None)

    async def close(self) -> None:
        async with self._connect_lock:
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        task = self._reader_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        websocket = self._websocket
        self._websocket = None
        self._reader_task = None
        self._send_cipher = None
        self._receive_cipher = None
        self._initiator = None
        self._assembler = FrameAssembler()
        for queue in tuple(self._pending.values()):
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                queue.put_nowait(
                    GatewayFailure(LinkErrorCode.MUSE_CONNECT_FAILED, may_have_been_sent=True)
                )
            except asyncio.QueueFull:
                pass
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                pass
