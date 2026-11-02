"""Journaled, provider-agnostic orchestration for native agent links."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Awaitable, Callable, Mapping

from .codes import AgentLinkFailure, LinkErrorCode, normalize_code
from .contracts import (
    AgentLink,
    Channel,
    Observation,
    Phase,
    PreparedSend,
    SendReceipt,
    TERMINAL_PHASES,
    TurnRef,
    nonce_sha256,
)
from .journal import JournalRecord, SQLiteAgentLinkJournal, valid_settlement


def _channel_code(channel: Channel, *, sent: bool) -> LinkErrorCode:
    if sent:
        return (
            LinkErrorCode.MUSE_SEND_UNCERTAIN
            if channel is Channel.MUSE
            else LinkErrorCode.GROK_SEND_UNCERTAIN
        )
    return (
        LinkErrorCode.MUSE_REPLY_PENDING
        if channel is Channel.MUSE
        else LinkErrorCode.GROK_REPLY_PENDING
    )


def _known_rejection(code: LinkErrorCode, channel: Channel) -> bool:
    if code in {LinkErrorCode.GROK_SEND_REJECTED, LinkErrorCode.GROK_GATEWAY_REJECTED}:
        return channel is Channel.GROK_BOT
    return code is LinkErrorCode.MUSE_SEND_REJECTED and channel is Channel.MUSE


def _observation_for_record(
    record: JournalRecord,
    *,
    reply_text: str | None = None,
    complete: bool | None = None,
    complete_basis: str | None = None,
) -> Observation:
    terminal = record.phase in TERMINAL_PHASES
    proof = valid_settlement(record, record.remote_ids) and (
        (record.phase is Phase.REPLIED and record.remote_ids.get("settlement_outcome") == "success")
        or (record.phase is Phase.FAILED and record.remote_ids.get("settlement_outcome") in {"cancelled", "error"})
    )
    if record.channel is Channel.GROK_BOT and terminal:
        return Observation(phase=record.phase, reply_text=reply_text, complete=proof,
                           complete_basis="provider_terminal" if proof else None,
                           code=record.code.value if record.code else None,
                           remote_ids=record.remote_ids if proof else {})
    return Observation(
        phase=record.phase,
        reply_text=reply_text,
        complete=terminal if complete is None else complete,
        complete_basis=complete_basis or ("journal_terminal" if terminal else None),
        code=record.code.value if record.code is not None else None,
    )


class AgentLinkService:
    """Coordinate a link without ever retrying an uncertain send.

    ``clock`` is monotonic and controls the bounded foreground wait.
    ``wall_clock`` supplies durable journal timestamps. Both are injectable so
    fake-clock tests do not wait in real time.
    """

    def __init__(
        self,
        journal: SQLiteAgentLinkJournal,
        *,
        wait_window_s: float = 90.0,
        recovery_window_s: float = 30 * 60.0,
        recover_limit: int = 100,
        initial_poll_s: float = 2.0,
        max_poll_s: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self.journal = journal
        self.wait_window_s = self._positive(wait_window_s, "wait window")
        self.recovery_window_s = self._positive(
            recovery_window_s, "recovery window"
        )
        if type(recover_limit) is not int or not 1 <= recover_limit <= 1000:
            raise ValueError("recover limit is invalid")
        self.recover_limit = recover_limit
        self.initial_poll_s = self._positive(initial_poll_s, "initial poll interval")
        self.max_poll_s = self._positive(max_poll_s, "maximum poll interval")
        if self.max_poll_s < self.initial_poll_s:
            raise ValueError("maximum poll interval is invalid")
        self.clock = clock
        self.wall_clock = wall_clock
        self.sleep = sleep
        self._next_poll_at: dict[str, float] = {}
        self._last_recover_diagnostics: dict[str, int] = {}

    @property
    def last_recover_diagnostics(self) -> Mapping[str, int]:
        """Fixed-code counts from the latest recover pass; it contains no IDs."""

        return dict(self._last_recover_diagnostics)

    @staticmethod
    def _positive(value: object, name: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{name} is invalid") from None
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{name} is invalid")
        return number

    @staticmethod
    def _safe_code(error: BaseException, fallback: LinkErrorCode) -> LinkErrorCode:
        # Never read or serialize exception text. Only a known .code attribute
        # may be projected to the closed vocabulary.
        return normalize_code(getattr(error, "code", fallback))

    @staticmethod
    def _same_turn(record: JournalRecord, turn: TurnRef) -> bool:
        return SQLiteAgentLinkJournal.matches_turn(record, turn)

    def _receipt_from_record(self, turn: TurnRef, record: JournalRecord) -> SendReceipt:
        if record.phase is Phase.PREPARED:
            # A surviving PREPARED row may have crashed on either side of the
            # send call. Resolve conservatively to UNCERTAIN and never resend.
            self.journal.transition(
                record.turn_id,
                expected=Phase.PREPARED,
                phase=Phase.UNCERTAIN,
                now=self.wall_clock(),
                code=LinkErrorCode.LINK_JOURNAL_CONFLICT,
            )
            refreshed = self.journal.get(record.turn_id)
            if refreshed is not None:
                record = refreshed
        return SendReceipt(
            turn=turn,
            phase=record.phase,
            code=record.code.value if record.code else None,
            remote_ids=record.remote_ids,
            baseline=record.baseline,
        )

    async def prepare_and_send(
        self, link: AgentLink, turn: TurnRef, text: str
    ) -> SendReceipt:
        """Persist PREPARED, then send once; any ambiguous send stays UNCERTAIN."""

        if not isinstance(text, str):
            return SendReceipt(
                turn=turn,
                phase=Phase.REJECTED,
                code=LinkErrorCode.LINK_INTERNAL.value,
            )
        existing = self.journal.get(turn.turn_id)
        if existing is not None:
            if not self._same_turn(existing, turn):
                return SendReceipt(
                    turn=turn,
                    phase=Phase.UNCERTAIN,
                    code=LinkErrorCode.LINK_JOURNAL_CONFLICT.value,
                )
            return self._receipt_from_record(turn, existing)

        try:
            prepared = await link.prepare(turn, text)
            if (
                not isinstance(prepared, PreparedSend)
                or prepared.turn != turn
            ):
                raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL)
            now = self.wall_clock()
            created, record = self.journal.create_prepared(
                turn,
                baseline=prepared.baseline,
                now=now,
                expires_at=now + self.recovery_window_s,
            )
            if not created:
                if not self._same_turn(record, turn):
                    return SendReceipt(
                        turn=turn,
                        phase=Phase.UNCERTAIN,
                        code=LinkErrorCode.LINK_JOURNAL_CONFLICT.value,
                    )
                return self._receipt_from_record(turn, record)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            code = self._safe_code(error, LinkErrorCode.LINK_INTERNAL)
            # A prepare failure occurs before the send call. Persist a terminal
            # rejection so replaying the same turn ID cannot later send it.
            try:
                now = self.wall_clock()
                created, record = self.journal.create_prepared(
                    turn,
                    baseline={},
                    now=now,
                    expires_at=now + self.recovery_window_s,
                )
                if created:
                    self.journal.transition(
                        turn.turn_id,
                        expected=Phase.PREPARED,
                        phase=Phase.REJECTED,
                        now=self.wall_clock(),
                        code=code,
                    )
                    record = self.journal.get(turn.turn_id) or record
                return self._receipt_from_record(turn, record)
            except BaseException:
                return SendReceipt(
                    turn=turn,
                    phase=Phase.REJECTED,
                    code=LinkErrorCode.LINK_INTERNAL.value,
                )

        try:
            receipt = await link.send(prepared)
            if not isinstance(receipt, SendReceipt) or receipt.turn != turn:
                raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL)
            phase = Phase(receipt.phase)
            if phase not in {Phase.SENT, Phase.UNCERTAIN, Phase.REJECTED}:
                raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL)
            code = normalize_code(receipt.code) if receipt.code is not None else None
            if phase is Phase.UNCERTAIN and code is None:
                code = _channel_code(turn.channel, sent=True)
            if phase is Phase.REJECTED and code is None:
                code = (
                    LinkErrorCode.MUSE_SEND_REJECTED
                    if turn.channel is Channel.MUSE
                    else LinkErrorCode.GROK_SEND_REJECTED
                )
            updated = self.journal.transition(
                turn.turn_id,
                expected=Phase.PREPARED,
                phase=phase,
                now=self.wall_clock(),
                code=code,
                remote_ids=receipt.remote_ids,
            )
            record = self.journal.get(turn.turn_id)
            if not updated and record is not None:
                return self._receipt_from_record(turn, record)
            if record is not None:
                return self._receipt_from_record(turn, record)
            return SendReceipt(
                turn=turn,
                phase=Phase.UNCERTAIN,
                code=LinkErrorCode.LINK_INTERNAL.value,
                baseline=prepared.baseline,
            )
        except asyncio.CancelledError:
            try:
                self.journal.transition(
                    turn.turn_id,
                    expected=Phase.PREPARED,
                    phase=Phase.UNCERTAIN,
                    now=self.wall_clock(),
                    code=_channel_code(turn.channel, sent=True),
                )
            except BaseException:
                pass
            raise
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            code = self._safe_code(error, _channel_code(turn.channel, sent=True))
            rejected = _known_rejection(code, turn.channel)
            phase = Phase.REJECTED if rejected else Phase.UNCERTAIN
            if not rejected and code is LinkErrorCode.LINK_INTERNAL:
                code = _channel_code(turn.channel, sent=True)
            try:
                self.journal.transition(
                    turn.turn_id,
                    expected=Phase.PREPARED,
                    phase=phase,
                    now=self.wall_clock(),
                    code=code,
                )
                record = self.journal.get(turn.turn_id)
                if record is not None:
                    return self._receipt_from_record(turn, record)
            except BaseException:
                pass
            return SendReceipt(
                turn=turn,
                phase=Phase.UNCERTAIN,
                code=LinkErrorCode.LINK_INTERNAL.value,
                baseline=prepared.baseline,
            )

    async def observe_once(self, link: AgentLink, turn: TurnRef) -> Observation:
        """Observe a turn once and CAS its terminal state at most once."""

        try:
            record = self.journal.get(turn.turn_id)
        except BaseException:
            return Observation(
                phase=Phase.UNCERTAIN,
                code=LinkErrorCode.LINK_INTERNAL.value,
            )
        if record is None or not self._same_turn(record, turn):
            return Observation(
                phase=Phase.UNCERTAIN,
                code=LinkErrorCode.LINK_JOURNAL_CONFLICT.value,
            )
        from .grok_native.link import GrokLink

        if record.channel is Channel.GROK_BOT and record.phase is Phase.REPLIED:
            saved = _observation_for_record(record)
            if saved.complete and record.delivered_at is not None:
                return saved
            if saved.complete:
                reread = await self._reobserve_undelivered_reply(link, turn, record)
                return _observation_for_record(record, reply_text=reread.reply_text)
            if not saved.complete:
                if not isinstance(link, GrokLink) or record.expires_at <= self.wall_clock():
                    return saved
                try:
                    observed = await link.observe(turn, record.baseline)
                    if (observed.complete and observed.complete_basis == "provider_terminal"
                            and observed.phase in {Phase.REPLIED, Phase.FAILED}):
                        won = self.journal.settle_grok(turn, record, phase=observed.phase,
                                                       proof=observed.remote_ids, now=self.wall_clock(),
                                                       code=observed.code)
                        latest = self.journal.get(turn.turn_id)
                        if latest is not None and self._same_turn(latest, turn):
                            return _observation_for_record(
                                latest, reply_text=(observed.reply_text
                                                    if won and latest.phase is Phase.REPLIED else None)
                            )
                except Exception:
                    pass
                return saved
        if record.phase is Phase.REPLIED and record.delivered_at is None:
            if record.expires_at <= self.wall_clock():
                return Observation(
                    phase=Phase.REPLIED,
                    complete=False,
                    code=LinkErrorCode.LINK_REPLY_UNDELIVERED_EXPIRED.value,
                )
            return await self._reobserve_undelivered_reply(link, turn, record)
        if record.phase in TERMINAL_PHASES:
            return _observation_for_record(record)
        now = self.wall_clock()
        if record.expires_at <= now:
            try:
                self.journal.transition(
                    record.turn_id,
                    expected=record.phase,
                    phase=Phase.EXPIRED,
                    now=now,
                    code=LinkErrorCode.LINK_WINDOW_EXPIRED,
                )
                refreshed = self.journal.get(record.turn_id)
                if refreshed is not None:
                    return _observation_for_record(refreshed)
            except BaseException:
                pass
            return Observation(
                phase=Phase.UNCERTAIN,
                code=LinkErrorCode.LINK_INTERNAL.value,
            )
        if record.phase is Phase.PREPARED:
            try:
                self.journal.transition(
                    record.turn_id,
                    expected=Phase.PREPARED,
                    phase=Phase.UNCERTAIN,
                    now=now,
                    code=LinkErrorCode.LINK_JOURNAL_CONFLICT,
                )
                record = self.journal.get(record.turn_id) or record
            except BaseException:
                return Observation(
                    phase=Phase.UNCERTAIN,
                    code=LinkErrorCode.LINK_INTERNAL.value,
                )

        try:
            outcome = await link.observe(turn, record.baseline)
            if not isinstance(outcome, Observation):
                raise AgentLinkFailure(LinkErrorCode.LINK_INTERNAL)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            code = self._safe_code(error, LinkErrorCode.LINK_INTERNAL)
            return Observation(
                phase=record.phase,
                complete=False,
                code=code.value,
                next_poll_after_s=self.initial_poll_s,
            )

        target_phase: Phase | None = None
        code = normalize_code(outcome.code) if outcome.code is not None else None
        if outcome.complete and outcome.phase is Phase.REPLIED:
            target_phase = Phase.REPLIED
        elif outcome.complete and outcome.phase is Phase.FAILED:
            target_phase = Phase.FAILED
        if target_phase is None:
            return Observation(
                phase=record.phase,
                reply_text=outcome.reply_text,
                complete=False,
                complete_basis=outcome.complete_basis,
                code=code.value if code else None,
                next_poll_after_s=outcome.next_poll_after_s,
            )

        if turn.channel is Channel.GROK_BOT and isinstance(link, GrokLink):
            if (outcome.complete_basis != "provider_terminal"
                    or not valid_settlement(record, outcome.remote_ids)):
                return Observation(phase=record.phase, reply_text=outcome.reply_text,
                                   complete=False, next_poll_after_s=outcome.next_poll_after_s)

        try:
            won = self.journal.settle_grok(
                turn, record, phase=target_phase, proof=outcome.remote_ids,
                now=self.wall_clock(), code=code,
            ) if turn.channel is Channel.GROK_BOT and isinstance(link, GrokLink) else self.journal.transition(
                record.turn_id,
                expected=(Phase.SENT, Phase.UNCERTAIN),
                phase=target_phase,
                now=self.wall_clock(),
                code=code,
            )
        except BaseException:
            return Observation(
                phase=record.phase,
                complete=False,
                code=LinkErrorCode.LINK_INTERNAL.value,
                next_poll_after_s=self.initial_poll_s,
            )
        if not won:
            latest = self.journal.get(record.turn_id)
            return (
                _observation_for_record(latest)
                if latest is not None and self._same_turn(latest, turn)
                else Observation(phase=Phase.UNCERTAIN, code=LinkErrorCode.LINK_INTERNAL.value)
            )
        latest = self.journal.get(record.turn_id)
        if latest is None or not self._same_turn(latest, turn):
            return Observation(
                phase=Phase.UNCERTAIN,
                code=LinkErrorCode.LINK_INTERNAL.value,
            )
        return Observation(
            phase=latest.phase,
            reply_text=outcome.reply_text if target_phase is Phase.REPLIED else None,
            complete=True,
            complete_basis=(outcome.complete_basis if turn.channel is not Channel.GROK_BOT
                            or isinstance(link, GrokLink) else "journal_terminal"),
            code=latest.code.value if latest.code else None,
            next_poll_after_s=outcome.next_poll_after_s,
            remote_ids=latest.remote_ids if turn.channel is Channel.GROK_BOT else {},
        )

    async def _reobserve_undelivered_reply(
        self,
        link: AgentLink,
        turn: TurnRef,
        record: JournalRecord,
    ) -> Observation:
        """Re-read a completed reply until the caller acknowledges persistence."""

        try:
            outcome = await link.observe(turn, record.baseline)
            if (
                not isinstance(outcome, Observation)
                or outcome.phase is not Phase.REPLIED
                or not outcome.complete
                or not isinstance(outcome.reply_text, str)
                or not outcome.reply_text
            ):
                return Observation(
                    phase=Phase.REPLIED,
                    complete=False,
                    code=(
                        normalize_code(outcome.code).value
                        if isinstance(outcome, Observation) and outcome.code
                        else LinkErrorCode.LINK_INTERNAL.value
                    ),
                    next_poll_after_s=(
                        outcome.next_poll_after_s
                        if isinstance(outcome, Observation)
                        else self.initial_poll_s
                    ),
                )
            return Observation(
                phase=Phase.REPLIED,
                reply_text=outcome.reply_text,
                complete=True,
                complete_basis=outcome.complete_basis,
                code=outcome.code,
                next_poll_after_s=outcome.next_poll_after_s,
                remote_ids=record.remote_ids if valid_settlement(record, record.remote_ids) else {},
            )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return Observation(
                phase=Phase.REPLIED,
                complete=False,
                code=self._safe_code(error, LinkErrorCode.LINK_INTERNAL).value,
                next_poll_after_s=self.initial_poll_s,
            )

    def acknowledge_delivery(self, turn: TurnRef) -> bool:
        """Mark REPLIED delivered only after the caller persisted its reply."""

        return self.journal.acknowledge_delivery(turn, now=self.wall_clock())

    async def wait_for_reply(self, link: AgentLink, turn: TurnRef) -> Observation:
        """Poll within the bounded foreground window; expiry here stays pending."""

        deadline = self.clock() + self.wait_window_s
        delay = self.initial_poll_s
        latest = Observation(phase=Phase.SENT, next_poll_after_s=delay)
        while self.clock() < deadline:
            latest = await self.observe_once(link, turn)
            if latest.phase in TERMINAL_PHASES:
                return latest
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            hinted = latest.next_poll_after_s
            pause = max(delay, hinted) if hinted > 0 else delay
            pause = min(max(pause, 0.01), remaining)
            await self.sleep(pause)
            delay = min(delay * 2.0, self.max_poll_s)
        record = self.journal.get(turn.turn_id)
        if record is None or not self._same_turn(record, turn):
            return Observation(
                phase=Phase.UNCERTAIN,
                code=LinkErrorCode.LINK_JOURNAL_CONFLICT.value,
            )
        return Observation(
            phase=record.phase,
            complete=False,
            complete_basis=latest.complete_basis,
            code=latest.code
            or _channel_code(
                turn.channel, sent=record.phase is Phase.UNCERTAIN
            ).value,
            next_poll_after_s=latest.next_poll_after_s,
        )

    async def recover(
        self,
        links: Mapping[Channel | str, AgentLink],
        now: float | None = None,
    ) -> list[Observation]:
        """Observe due recoverable rows without sending; expire rows past window."""

        current = self.wall_clock() if now is None else float(now)
        observations: list[Observation] = []
        self._last_recover_diagnostics = {}
        try:
            for record in self.journal.list_expired(
                current, limit=self.recover_limit
            ):
                self.journal.transition(
                    record.turn_id,
                    expected=record.phase,
                    phase=Phase.EXPIRED,
                    now=current,
                    code=LinkErrorCode.LINK_WINDOW_EXPIRED,
                )
            recoverable = self.journal.list_recoverable(
                current, limit=self.recover_limit
            )
            undelivered = self.journal.list_undelivered_replies(
                current, limit=self.recover_limit
            )
            expired_undelivered = self.journal.list_undelivered_replies(
                current, expired=True, limit=self.recover_limit
            )
            if expired_undelivered:
                self._last_recover_diagnostics[
                    LinkErrorCode.LINK_REPLY_UNDELIVERED_EXPIRED.value
                ] = len(expired_undelivered)
        except BaseException:
            return [
                Observation(
                    phase=Phase.UNCERTAIN,
                    code=LinkErrorCode.LINK_INTERNAL.value,
                )
            ]

        candidates = sorted(
            [*recoverable, *undelivered],
            key=lambda row: (row.created_at, row.turn_id),
        )[: self.recover_limit]
        for record in candidates:
            due_at = self._next_poll_at.get(record.turn_id, float("-inf"))
            if current < due_at:
                continue
            link = links.get(record.channel) or links.get(record.channel.value)
            if link is None:
                continue
            turn = TurnRef(
                turn_id=record.turn_id,
                channel=record.channel,
                owner_id=record.owner_id,
                thread_ref=record.thread_ref,
            )
            if nonce_sha256(turn.nonce) != record.nonce_sha256:
                observations.append(
                    Observation(
                        phase=record.phase,
                        code=LinkErrorCode.LINK_JOURNAL_CONFLICT.value,
                        next_poll_after_s=self.max_poll_s,
                    )
                )
                continue
            outcome = await self.observe_once(link, turn)
            observations.append(outcome)
            interval = outcome.next_poll_after_s or self.initial_poll_s
            self._next_poll_at[record.turn_id] = current + max(0.01, interval)
        if len(self._next_poll_at) > 2048:
            live_ids = {record.turn_id for record in recoverable}
            self._next_poll_at = {
                turn_id: due_at
                for turn_id, due_at in self._next_poll_at.items()
                if turn_id in live_ids
            }
        return observations
