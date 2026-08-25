"""Semantic utterance segmentation for streaming LLM text → TTS."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass


_CJK_SENTENCE_END_RE = re.compile(r"[。．！？!?]+")
_EN_SENTENCE_END_RE = re.compile(r"(?<=[^\d])\.(?=\s|$)")
_MAX_BUFFER_CHARS = 4096


@dataclass(frozen=True)
class Utterance:
    id: str
    text: str


class UtteranceSegmenter:
    """Buffer text deltas and emit speakable utterance segments."""

    def __init__(
        self,
        *,
        max_chars: int = 180,
        max_wait_ms: int = 450,
    ) -> None:
        self.max_chars = max(1, int(max_chars))
        self.max_wait_ms = max(50, int(max_wait_ms))
        self._buffer = ""
        self._last_push_at: float | None = None
        self._generation_token = 0

    def reset(self) -> None:
        self._buffer = ""
        self._last_push_at = None
        self._generation_token += 1

    def push(self, delta: str, now: float | None = None) -> list[Utterance]:
        if not delta:
            return []
        current_now = now if now is not None else time.monotonic()
        utterances: list[Utterance] = []
        if (
            self._buffer.strip()
            and self._last_push_at is not None
            and (current_now - self._last_push_at) * 1000.0 >= self.max_wait_ms
        ):
            utterances.extend(self._drain(force=True, now=current_now))
        if len(self._buffer) + len(delta) > _MAX_BUFFER_CHARS:
            self._buffer = self._buffer[-_MAX_BUFFER_CHARS // 2 :]
        self._buffer += delta
        self._last_push_at = current_now
        utterances.extend(self._drain(force=False, now=current_now))
        return utterances

    def flush(self) -> list[Utterance]:
        return self._drain(force=True, now=time.monotonic())

    def pending_chars(self) -> int:
        return len(self._buffer)

    def _drain(self, *, force: bool, now: float) -> list[Utterance]:
        utterances: list[Utterance] = []
        while self._buffer:
            emitted = self._try_emit_sentence_boundary()
            if emitted is not None:
                utterances.append(emitted)
                continue
            if len(self._buffer) >= self.max_chars:
                chunk = self._buffer[: self.max_chars]
                self._buffer = self._buffer[self.max_chars :]
                chunk = chunk.strip()
                if chunk:
                    utterances.append(Utterance(id=self._new_id(), text=chunk))
                continue
            if (
                force
                or (
                    self._last_push_at is not None
                    and (now - self._last_push_at) * 1000.0 >= self.max_wait_ms
                    and self._buffer.strip()
                )
            ):
                chunk = self._buffer.strip()
                self._buffer = ""
                if chunk:
                    utterances.append(Utterance(id=self._new_id(), text=chunk))
                break
            break
        return utterances

    def _try_emit_sentence_boundary(self) -> Utterance | None:
        earliest: re.Match[str] | None = None
        for pattern in (_EN_SENTENCE_END_RE, _CJK_SENTENCE_END_RE):
            match = pattern.search(self._buffer)
            if match is not None and (earliest is None or match.start() < earliest.start()):
                earliest = match
        if earliest is None:
            return None
        end = earliest.end()
        chunk = self._buffer[:end].strip()
        self._buffer = self._buffer[end:]
        if not chunk:
            return None
        return Utterance(id=self._new_id(), text=chunk)

    def _new_id(self) -> str:
        return f"utt_{uuid.uuid4().hex[:12]}"


__all__ = ["Utterance", "UtteranceSegmenter"]
