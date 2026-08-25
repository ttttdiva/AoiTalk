"""Per-response generation state for Voice Session interruption."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class GenerationPhase(StrEnum):
    IDLE = "idle"
    GENERATING = "generating"
    PLAYING = "playing"
    INTERRUPTING = "interrupting"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class VoiceGenerationState:
    generation_id: str
    response_id: str | None = None
    item_id: str | None = None
    full_text: str = ""
    spoken_text: str = ""
    highest_acked_sequence: int = -1
    inflight_sequence: int | None = None
    phase: GenerationPhase = GenerationPhase.IDLE
    playback_partial_unknown: bool = False
    interrupted_before_playback: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def mint(cls, *, response_id: str | None = None) -> "VoiceGenerationState":
        return cls(
            generation_id=f"vg_{uuid.uuid4().hex}",
            response_id=response_id,
            phase=GenerationPhase.GENERATING,
        )


@dataclass
class InterruptResult:
    generation: VoiceGenerationState
    spoken_text: str
    full_text: str
    committed: bool
    reconciliation_required: bool = False
    fail_closed: bool = False


async def interrupt_generation(
    state: VoiceGenerationState,
    *,
    lock: asyncio.Lock | None = None,
    spoken_prefix: str = "",
    partial_unknown: bool = False,
    before_playback: bool = False,
) -> InterruptResult:
    """Idempotent interrupt within a generation lock."""

    async def _apply() -> InterruptResult:
        if state.phase in {GenerationPhase.INTERRUPTED, GenerationPhase.FAILED, GenerationPhase.COMPLETED}:
            return InterruptResult(
                generation=state,
                spoken_text=state.spoken_text,
                full_text=state.full_text,
                committed=state.phase == GenerationPhase.INTERRUPTED,
            )
        state.phase = GenerationPhase.INTERRUPTING
        if before_playback or (spoken_prefix == "" and state.highest_acked_sequence < 0):
            state.interrupted_before_playback = True
            state.spoken_text = ""
        else:
            state.spoken_text = spoken_prefix
            state.playback_partial_unknown = partial_unknown
        state.phase = GenerationPhase.INTERRUPTED
        reconciliation_required = bool(state.full_text and state.spoken_text != state.full_text)
        return InterruptResult(
            generation=state,
            spoken_text=state.spoken_text,
            full_text=state.full_text,
            committed=not state.interrupted_before_playback,
            reconciliation_required=reconciliation_required,
        )

    if lock is None:
        return await _apply()
    async with lock:
        return await _apply()


__all__ = [
    "GenerationPhase",
    "InterruptResult",
    "VoiceGenerationState",
    "interrupt_generation",
]
