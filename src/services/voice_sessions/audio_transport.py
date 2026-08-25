"""Browser custom TTS audio transport over WebSocket."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


MAX_AUDIO_FRAME_BYTES = 8 * 1024 * 1024
PHASE1_QUEUE_MAXSIZE = 2


@dataclass
class PendingAudioSegment:
    generation_id: str
    sequence: int
    segment_id: str
    mime_type: str
    audio: bytes


@dataclass
class VoiceAudioTransportState:
    generation_id: str
    next_sequence: int = 0
    highest_acked: int = -1
    inflight_sequence: int | None = None
    closed: bool = False
    cleared: bool = False


SendJson = Callable[[dict[str, Any]], Awaitable[None]]
SendBinary = Callable[[bytes], Awaitable[None]]
OnAck = Callable[[str, int], Awaitable[None]]


class VoiceAudioTransportManager:
    """Bounded queue with ACK-based backpressure for custom TTS playback."""

    def __init__(
        self,
        *,
        unacked_window: int = 1,
        on_ack: OnAck | None = None,
        queue_maxsize: int = PHASE1_QUEUE_MAXSIZE,
    ) -> None:
        self.unacked_window = max(1, int(unacked_window))
        self._on_ack = on_ack
        self._state: VoiceAudioTransportState | None = None
        self._queue: asyncio.Queue[PendingAudioSegment | None] = asyncio.Queue(
            maxsize=max(1, int(queue_maxsize))
        )
        self._sender_task: asyncio.Task[Any] | None = None
        self._send_json: SendJson | None = None
        self._send_binary: SendBinary | None = None
        self._connection_bound = False
        self._clear_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_complete = False

    def bind_connection(
        self,
        *,
        send_json: SendJson,
        send_binary: SendBinary,
    ) -> None:
        self._send_json = send_json
        self._send_binary = send_binary
        self._connection_bound = True

    def unbind_connection(self) -> None:
        self._connection_bound = False
        self._send_json = None
        self._send_binary = None
        state = self._state
        if state is not None:
            state.inflight_sequence = None

    def is_connection_bound(self) -> bool:
        return self._connection_bound

    def set_on_ack(self, on_ack: OnAck | None) -> None:
        self._on_ack = on_ack

    async def activate_generation(self, generation_id: str) -> None:
        send_json = self._send_json
        send_binary = self._send_binary
        if not self._connection_bound or send_json is None or send_binary is None:
            return
        self.attach(
            generation_id=generation_id,
            send_json=send_json,
            send_binary=send_binary,
        )
        await send_json({"type": "audio.generation", "generation_id": generation_id})

    def attach(
        self,
        *,
        generation_id: str,
        send_json: SendJson,
        send_binary: SendBinary,
    ) -> None:
        self._close_complete = False
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._state = VoiceAudioTransportState(generation_id=generation_id)
        self._send_json = send_json
        self._send_binary = send_binary
        self._connection_bound = True
        if self._sender_task is None or self._sender_task.done():
            self._sender_task = asyncio.create_task(self._run_sender())

    async def close(self) -> None:
        """Close transport idempotently and never block on a full queue.

        A sender can be waiting for the browser ACK while the session teardown
        runs, so awaiting ``queue.put(None)`` would deadlock once the bounded
        queue is full.  Marking the state closed makes the sender stop on its
        next check; a short bounded wait plus cancellation handles a stuck send.
        The current sender task is deliberately not awaited (self-await).
        """

        current = asyncio.current_task()
        async with self._close_lock:
            if self._close_complete:
                return
            self._close_complete = True
            state = self._state
            if state is not None:
                state.closed = True
                state.inflight_sequence = None
            self._connection_bound = False
            self._send_json = None
            self._send_binary = None
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            task = self._sender_task

        if task is not None and task is not current and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
            except BaseException:
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
        if task is not current:
            self._sender_task = None
        self._state = None

    async def enqueue_segment(
        self,
        *,
        generation_id: str,
        segment_id: str,
        mime_type: str,
        audio: bytes,
    ) -> int | None:
        state = self._state
        if state is None or state.closed or generation_id != state.generation_id:
            return None
        if state.cleared:
            return None
        if len(audio) > MAX_AUDIO_FRAME_BYTES:
            raise ValueError("audio segment exceeds transport limit")
        sequence = state.next_sequence
        state.next_sequence += 1
        await self._queue.put(
            PendingAudioSegment(
                generation_id=generation_id,
                sequence=sequence,
                segment_id=segment_id,
                mime_type=mime_type,
                audio=audio,
            )
        )
        return sequence

    async def handle_client_message(self, payload: dict[str, Any]) -> None:
        state = self._state
        if state is None or state.closed:
            return
        message_type = str(payload.get("type") or "")
        if message_type == "ack":
            message_type = "audio.ack"
        if message_type != "audio.ack":
            return
        generation_id = str(payload.get("generation_id") or "")
        if generation_id != state.generation_id:
            return
        try:
            sequence = int(payload.get("sequence"))
        except (TypeError, ValueError):
            return
        if sequence <= state.highest_acked:
            return
        if state.inflight_sequence is None or sequence != state.inflight_sequence:
            return
        state.highest_acked = sequence
        state.inflight_sequence = None
        callback = self._on_ack
        if callback is not None:
            await callback(generation_id, sequence)

    async def clear_generation(self, *, generation_id: str, reason: str) -> None:
        state = self._state
        if state is None or generation_id != state.generation_id:
            return
        async with self._clear_lock:
            state.cleared = True
            state.inflight_sequence = None
            preserved: list[PendingAudioSegment] = []
            while True:
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    continue
                if item.generation_id != generation_id:
                    preserved.append(item)
            for item in preserved:
                await self._queue.put(item)
            send_json = self._send_json
            if send_json is not None:
                await send_json(
                    {
                        "type": "audio.clear",
                        "generation_id": generation_id,
                        "reason": reason,
                    }
                )

    def spoken_prefix_from_acks(self, segments: dict[int, str]) -> str:
        state = self._state
        if state is None:
            return ""
        parts: list[str] = []
        for sequence in sorted(segments):
            if sequence <= state.highest_acked:
                parts.append(segments[sequence])
        return "".join(parts).strip()

    def inflight_sequence(self) -> int | None:
        state = self._state
        if state is None:
            return None
        return state.inflight_sequence

    def highest_acked(self) -> int:
        state = self._state
        if state is None:
            return -1
        return state.highest_acked

    async def _run_sender(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                state = self._state
                send_json = self._send_json
                send_binary = self._send_binary
                if (
                    state is None
                    or state.closed
                    or state.cleared
                    or send_json is None
                    or send_binary is None
                    or item.generation_id != state.generation_id
                ):
                    continue
                while state.inflight_sequence is not None:
                    await asyncio.sleep(0.01)
                    if state.closed or state.cleared:
                        return
                if (
                    state is None
                    or state.closed
                    or state.cleared
                    or item.generation_id != state.generation_id
                ):
                    continue
                state.inflight_sequence = item.sequence
                await send_json(
                    {
                        "type": "audio.segment",
                        "generation_id": item.generation_id,
                        "sequence": item.sequence,
                        "segment_id": item.segment_id,
                        "mime_type": item.mime_type,
                        "byte_length": len(item.audio),
                    }
                )
                await send_binary(item.audio)
        except asyncio.CancelledError:
            raise
        except Exception:
            state = self._state
            if state is not None:
                state.closed = True
            self._connection_bound = False
            raise


__all__ = ["VoiceAudioTransportManager", "MAX_AUDIO_FRAME_BYTES", "PHASE1_QUEUE_MAXSIZE"]
