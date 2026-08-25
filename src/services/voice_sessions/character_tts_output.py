"""Character TTS output path for realtime_character_tts mode."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Awaitable, Callable

from ...audio.utterance_segmenter import Utterance, UtteranceSegmenter
from ...tts.manager_factory import create_session_tts_manager
from ...tts.engine_bootstrap import bootstrap_session_tts_manager
from ...tts.manager import TTSManager
from .audio_transport import VoiceAudioTransportManager
from .generation import GenerationPhase, VoiceGenerationState
from .models import VoiceSessionPolicy

logger = logging.getLogger(__name__)

PlaybackCompleteCallback = Callable[[VoiceGenerationState], Awaitable[None]]
OutputFailureCallback = Callable[[VoiceGenerationState, str], Awaitable[None]]
_DRAIN_SENTINEL = object()


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    """Prevent detached cleanup tasks from producing unhandled exceptions."""

    try:
        task.result()
    except BaseException:
        pass


class CharacterTTSOutput:
    """Segment streaming assistant text and synthesize per utterance."""

    def __init__(
        self,
        *,
        policy: VoiceSessionPolicy,
        character_name: str,
        transport: VoiceAudioTransportManager,
        tts_factory: Callable[[], TTSManager] | None = None,
        on_playback_complete: PlaybackCompleteCallback | None = None,
        on_output_failure: OutputFailureCallback | None = None,
        app_config: Any | None = None,
    ) -> None:
        self.policy = policy
        self.character_name = character_name
        self.transport = transport
        self._on_playback_complete = on_playback_complete
        self._on_output_failure = on_output_failure
        self._app_config = app_config
        if tts_factory is not None:
            self._tts = tts_factory()
        else:
            self._tts = create_session_tts_manager(app_config)
        self._tts_ready = tts_factory is not None
        self._segmenter = UtteranceSegmenter(
            max_chars=policy.segment_max_chars,
            max_wait_ms=policy.segment_max_wait_ms,
        )
        self._generation: VoiceGenerationState | None = None
        self._segment_text_by_sequence: dict[int, str] = {}
        self._lock = asyncio.Lock()
        self._pending_segments: asyncio.Queue[Utterance | object | None] = asyncio.Queue(
            maxsize=max(1, policy.tts_queue_depth)
        )
        self._worker: asyncio.Task[Any] | None = None
        self._active_synthesis: asyncio.Task[Any] | None = None
        self._interrupt_token = 0
        self._max_wait_task: asyncio.Task[Any] | None = None
        self._buffer_deadline: float | None = None
        self._close_lock = asyncio.Lock()
        self._closed = False

    def bind_generation(self, generation: VoiceGenerationState) -> None:
        if self._closed:
            return
        self._generation = generation
        self._segmenter.reset()
        self._segment_text_by_sequence.clear()
        generation.metadata.setdefault("response_done", False)
        generation.metadata.setdefault("tts_drained", False)
        generation.metadata.setdefault("segments_enqueued", 0)
        generation.metadata.setdefault("tts_utterances_created", 0)
        generation.metadata.setdefault("tts_segments_synthesized", 0)
        generation.metadata.setdefault("audio_delivery_failed", False)
        generation.metadata.setdefault("durable_committed", False)
        self._interrupt_token += 1
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_worker())

    async def close(self) -> None:
        """Stop synthesis/output exactly once without awaiting the worker itself.

        Output failures are reported by the worker and the callback terminalizes
        the owning Live Voice session.  Session cleanup therefore re-enters this
        method from the worker task; awaiting that task would deadlock.  Mark the
        output closed and drain the queue while holding a short lock, then await
        only a different worker task outside the lock.
        """

        current = asyncio.current_task()
        cleanup_tts = False
        async with self._close_lock:
            if not self._closed:
                self._closed = True
                cleanup_tts = True
                self._cancel_max_wait_timer()
                self._interrupt_token += 1
                synthesis = self._active_synthesis
                if synthesis is not None and not synthesis.done():
                    synthesis.cancel()
                # ``put`` can block forever when the bounded queue is full and
                # the sender is waiting for an ACK.  Drop stale work and use a
                # non-blocking sentinel instead.
                while True:
                    try:
                        self._pending_segments.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    self._pending_segments.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            worker = self._worker

        if worker is not None and worker is not current and not worker.done():
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=0.5)
            except asyncio.TimeoutError:
                worker.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(worker), timeout=0.5)
                except BaseException:
                    if not worker.done():
                        worker.add_done_callback(_consume_task_result)
                    logger.warning("Character TTS worker did not stop during close")
            except BaseException:
                # Cancellation and provider/TTS failures are terminal cleanup
                # details; never let them strand the Realtime call teardown.
                pass
        self._worker = None if worker is None or worker is not current else worker
        self.transport.set_on_ack(None)
        try:
            await self.transport.close()
        except BaseException:
            logger.debug("Character TTS transport close skipped", exc_info=True)
        if cleanup_tts:
            cleanup = getattr(self._tts, "cleanup", None)
            if callable(cleanup):
                try:
                    result = cleanup()
                    if inspect.isawaitable(result):
                        await result
                except BaseException:
                    logger.debug("Character TTS manager cleanup skipped", exc_info=True)

    async def push_text_delta(self, delta: str) -> None:
        if self._closed:
            return
        generation = self._generation
        if generation is None or generation.phase in {
            GenerationPhase.INTERRUPTED,
            GenerationPhase.FAILED,
            GenerationPhase.COMPLETED,
        }:
            return
        generation.full_text += delta
        utterances = self._segmenter.push(delta)
        if self._segmenter.pending_chars() > 0:
            self._ensure_buffer_deadline()
        for utterance in utterances:
            await self._enqueue_utterance(utterance)
        self._schedule_max_wait_timer()

    async def flush(self) -> None:
        if self._closed:
            return
        self._cancel_max_wait_timer()
        for utterance in self._segmenter.flush():
            await self._enqueue_utterance(utterance)

    async def mark_response_done(self) -> None:
        if self._closed:
            return
        generation = self._generation
        if generation is None:
            return
        generation.metadata["response_done"] = True
        await self.flush()
        await self._enqueue_drain_sentinel()
        await self._maybe_complete_playback()

    async def mark_response_complete(self) -> None:
        await self.mark_response_done()

    def segments_enqueued(self) -> int:
        generation = self._generation
        if generation is None:
            return 0
        return int(generation.metadata.get("segments_enqueued") or 0)

    async def on_segment_acked(self, generation_id: str, sequence: int) -> None:
        await self._on_segment_acked(generation_id, sequence)

    async def reset_for_interrupt(self) -> None:
        if self._closed:
            return
        self._cancel_max_wait_timer()
        self._interrupt_token += 1
        self._segmenter.reset()
        synthesis = self._active_synthesis
        if synthesis is not None and not synthesis.done():
            synthesis.cancel()
        while not self._pending_segments.empty():
            try:
                self._pending_segments.get_nowait()
            except asyncio.QueueEmpty:
                break

    def spoken_prefix(self) -> tuple[str, bool]:
        generation = self._generation
        if generation is None:
            return "", False
        spoken = self.transport.spoken_prefix_from_acks(self._segment_text_by_sequence)
        partial_unknown = (
            generation.inflight_sequence is not None
            and generation.inflight_sequence > generation.highest_acked_sequence
        )
        return spoken, partial_unknown

    def _max_wait_seconds(self) -> float:
        return self.policy.segment_max_wait_ms / 1000.0

    def _ensure_buffer_deadline(self) -> float:
        if self._buffer_deadline is None:
            self._buffer_deadline = time.monotonic() + self._max_wait_seconds()
        return self._buffer_deadline

    def _clear_buffer_deadline(self) -> None:
        self._buffer_deadline = None

    def _remaining_buffer_deadline_seconds(self) -> float:
        if self._buffer_deadline is None:
            return self._max_wait_seconds()
        return max(0.0, self._buffer_deadline - time.monotonic())

    def _schedule_max_wait_timer(self) -> None:
        if self._closed:
            return
        if self._segmenter.pending_chars() <= 0:
            self._cancel_max_wait_timer()
            return
        self._ensure_buffer_deadline()
        if self._max_wait_task is not None and not self._max_wait_task.done():
            return
        self._max_wait_task = asyncio.create_task(self._max_wait_flush())

    def _cancel_max_wait_timer(self) -> None:
        task = self._max_wait_task
        if task is not None and not task.done():
            task.cancel()
        self._max_wait_task = None
        self._clear_buffer_deadline()

    async def _max_wait_flush(self) -> None:
        try:
            await asyncio.sleep(self._remaining_buffer_deadline_seconds())
            generation = self._generation
            if generation is None or generation.phase in {
                GenerationPhase.INTERRUPTED,
                GenerationPhase.FAILED,
                GenerationPhase.COMPLETED,
            }:
                return
            utterances = self._segmenter.flush()
            self._clear_buffer_deadline()
            for utterance in utterances:
                await self._enqueue_utterance(utterance)
        except asyncio.CancelledError:
            return
        finally:
            self._max_wait_task = None
            if not self._closed and self._segmenter.pending_chars() > 0:
                generation = self._generation
                if generation is not None and generation.phase not in {
                    GenerationPhase.INTERRUPTED,
                    GenerationPhase.FAILED,
                    GenerationPhase.COMPLETED,
                }:
                    self._schedule_max_wait_timer()

    async def _ensure_tts_ready(self) -> None:
        if self._tts_ready:
            return
        await bootstrap_session_tts_manager(
            self._tts,
            config=self._app_config,
            character_name=self.character_name,
        )
        self._tts_ready = True

    async def _enqueue_utterance(self, utterance: Utterance) -> None:
        if self._closed:
            return
        generation = self._generation
        if generation is not None:
            generation.metadata["tts_utterances_created"] = int(
                generation.metadata.get("tts_utterances_created") or 0
            ) + 1
        await self._pending_segments.put(utterance)

    async def _enqueue_drain_sentinel(self) -> None:
        if self._closed:
            return
        await self._pending_segments.put(_DRAIN_SENTINEL)

    async def _fail_output(self, generation: VoiceGenerationState, reason: str) -> None:
        if self._closed:
            return
        if generation.phase in {
            GenerationPhase.FAILED,
            GenerationPhase.COMPLETED,
            GenerationPhase.INTERRUPTED,
        }:
            return
        generation.metadata["audio_delivery_failed"] = True
        generation.phase = GenerationPhase.FAILED
        callback = self._on_output_failure
        if callback is not None:
            try:
                await callback(generation, reason)
            except Exception:
                logger.debug("Character TTS output failure callback skipped", exc_info=True)

    async def _on_segment_acked(self, generation_id: str, sequence: int) -> None:
        generation = self._generation
        if generation is None or generation.generation_id != generation_id:
            return
        generation.highest_acked_sequence = max(
            generation.highest_acked_sequence,
            sequence,
        )
        await self._maybe_complete_playback()

    async def _maybe_complete_playback(self) -> None:
        generation = self._generation
        if generation is None:
            return
        if generation.metadata.get("audio_delivery_failed"):
            return
        if not generation.metadata.get("response_done"):
            return
        if not generation.metadata.get("tts_drained"):
            return
        if generation.phase in {
            GenerationPhase.INTERRUPTED,
            GenerationPhase.FAILED,
            GenerationPhase.COMPLETED,
        }:
            return
        if generation.metadata.get("durable_committed"):
            return
        full_text = str(generation.full_text or "").strip()
        enqueued = int(generation.metadata.get("segments_enqueued") or 0)
        utterances_created = int(generation.metadata.get("tts_utterances_created") or 0)
        if full_text and utterances_created > 0 and enqueued == 0:
            await self._fail_output(generation, "zero_segments_delivered")
            return
        if full_text and enqueued == 0 and utterances_created == 0:
            await self._fail_output(generation, "non_empty_response_without_audio")
            return
        if enqueued > 0 and generation.highest_acked_sequence < enqueued - 1:
            return
        callback = self._on_playback_complete
        if callback is not None:
            try:
                await callback(generation)
            except Exception:
                logger.exception("Character TTS playback completion callback failed")
                await self._fail_output(generation, "playback_completion_exception")
                return
        generation.metadata["durable_committed"] = True
        generation.phase = GenerationPhase.COMPLETED

    async def _run_worker(self) -> None:
        while True:
            if self._closed:
                return
            item = await self._pending_segments.get()
            if item is None or self._closed:
                return
            generation = self._generation
            if generation is None:
                continue
            if item is _DRAIN_SENTINEL:
                if generation.metadata.get("response_done"):
                    generation.metadata["tts_drained"] = True
                    await self._maybe_complete_playback()
                continue
            utterance = item
            if not isinstance(utterance, Utterance):
                continue
            if generation.phase in {
                GenerationPhase.INTERRUPTING,
                GenerationPhase.INTERRUPTED,
                GenerationPhase.FAILED,
            }:
                continue
            if not str(utterance.text or "").strip():
                continue
            token = self._interrupt_token
            try:
                await self._ensure_tts_ready()
                self._active_synthesis = asyncio.create_task(
                    self._tts.synthesize(
                        utterance.text,
                        character_name=self.character_name,
                    )
                )
                audio = await self._active_synthesis
            except asyncio.CancelledError:
                continue
            except Exception:
                logger.exception("Character TTS synthesis failed")
                await self._fail_output(generation, "tts_synthesis_exception")
                return
            finally:
                self._active_synthesis = None
            if (
                self._generation is not generation
                or token != self._interrupt_token
                or generation.phase
                in {
                    GenerationPhase.INTERRUPTING,
                    GenerationPhase.INTERRUPTED,
                    GenerationPhase.FAILED,
                }
            ):
                continue
            if not audio:
                await self._fail_output(generation, "tts_synthesis_empty")
                return
            generation.metadata["tts_segments_synthesized"] = int(
                generation.metadata.get("tts_segments_synthesized") or 0
            ) + 1
            try:
                sequence = await self.transport.enqueue_segment(
                    generation_id=generation.generation_id,
                    segment_id=utterance.id,
                    mime_type="audio/wav",
                    audio=audio,
                )
            except Exception:
                logger.exception("Character TTS audio transport enqueue failed")
                await self._fail_output(generation, "audio_transport_enqueue_exception")
                return
            if sequence is None:
                await self._fail_output(generation, "audio_transport_rejected")
                return
            self._segment_text_by_sequence[sequence] = utterance.text
            generation.metadata["segments_enqueued"] = int(
                generation.metadata.get("segments_enqueued") or 0
            ) + 1
            generation.phase = GenerationPhase.PLAYING


__all__ = ["CharacterTTSOutput"]
