"""Scriptable in-process links for layer-one contract tests."""

from __future__ import annotations

from collections import deque
from typing import Iterable, Mapping

from .codes import LinkErrorCode
from .contracts import (
    Channel,
    LinkHealth,
    Observation,
    Phase,
    PreparedSend,
    SendReceipt,
    TurnRef,
)


class FakeAgentLink:
    """Deterministic fake; it never opens a socket, process, or database."""

    def __init__(
        self,
        channel: Channel | str,
        *,
        baseline: Mapping[str, object] | None = None,
        send_script: Iterable[Phase | SendReceipt | BaseException] = (Phase.SENT,),
        observations: Iterable[Observation | BaseException] = (),
        prepare_error: BaseException | None = None,
        send_error: BaseException | None = None,
        health_result: LinkHealth | None = None,
    ) -> None:
        self.channel = Channel(channel)
        self.baseline = dict(baseline or {"seq": 0})
        self.send_script = deque(send_script)
        self.observation_script = deque(observations)
        self.prepare_error = prepare_error
        self.send_error = send_error
        self.health_result = health_result or LinkHealth(healthy=True)
        self.prepare_calls = 0
        self.send_calls = 0
        self.observe_calls = 0
        self.health_calls = 0
        self.close_calls = 0

    async def prepare(self, turn: TurnRef, text: str) -> PreparedSend:
        self.prepare_calls += 1
        if self.prepare_error is not None:
            raise self.prepare_error
        if turn.channel is not self.channel:
            raise ValueError("fake channel mismatch")
        return PreparedSend(turn=turn, text=text, baseline=self.baseline)

    async def send(self, prepared: PreparedSend) -> SendReceipt:
        self.send_calls += 1
        if self.send_error is not None:
            raise self.send_error
        step = self.send_script.popleft() if self.send_script else Phase.SENT
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, SendReceipt):
            return step
        phase = Phase(step)
        code = None
        if phase is Phase.UNCERTAIN:
            code = (
                LinkErrorCode.MUSE_SEND_UNCERTAIN
                if self.channel is Channel.MUSE
                else LinkErrorCode.GROK_SEND_UNCERTAIN
            )
        elif phase is Phase.REJECTED:
            code = (
                LinkErrorCode.MUSE_SEND_REJECTED
                if self.channel is Channel.MUSE
                else LinkErrorCode.GROK_SEND_REJECTED
            )
        return SendReceipt(
            turn=prepared.turn,
            phase=phase,
            code=code.value if code else None,
            baseline=prepared.baseline,
        )

    async def observe(
        self, turn: TurnRef, baseline: Mapping[str, object]
    ) -> Observation:
        self.observe_calls += 1
        if turn.channel is not self.channel:
            raise ValueError("fake channel mismatch")
        if dict(baseline) != self.baseline:
            raise ValueError("fake baseline mismatch")
        if self.observation_script:
            step = self.observation_script.popleft()
            if isinstance(step, BaseException):
                raise step
            return step
        return Observation(
            phase=Phase.SENT,
            complete=False,
            next_poll_after_s=2.0,
        )

    async def health(self) -> LinkHealth:
        self.health_calls += 1
        return self.health_result

    async def close(self) -> None:
        self.close_calls += 1
