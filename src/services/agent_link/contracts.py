"""Provider-independent agent link contracts for the native link rollout."""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Protocol

from .codes import normalize_code


_TURN_NONCE_NAMESPACE = uuid.UUID("250551c4-65e8-5a79-b9b8-75fd7d7db75a")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SENSITIVE_MARKER = re.compile(
    r"(?:token|secret|credential|password|cookie|authorization|bearer|api[_-]?key)",
    re.IGNORECASE,
)


class Channel(StrEnum):
    MUSE = "muse"
    GROK_BOT = "grok_bot"


class Phase(StrEnum):
    PREPARED = "prepared"
    SENT = "sent"
    UNCERTAIN = "uncertain"
    REPLIED = "replied"
    REJECTED = "rejected"
    FAILED = "failed"
    EXPIRED = "expired"


TERMINAL_PHASES = frozenset(
    {Phase.REPLIED, Phase.REJECTED, Phase.FAILED, Phase.EXPIRED}
)


def nonce_for_turn_id(turn_id: str) -> str:
    """Regenerate a correlation nonce without persisting its plaintext.

    Turn IDs are random UUIDs. The domain-separated UUIDv5 is correlation
    material only; it is not an authentication credential.
    """

    try:
        canonical = str(uuid.UUID(str(turn_id)))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("turn_id must be a UUID") from None
    return uuid.uuid5(_TURN_NONCE_NAMESPACE, canonical).hex


def nonce_sha256(nonce: str) -> str:
    return hashlib.sha256(str(nonce).encode("utf-8")).hexdigest()


def _freeze_mapping(value: Mapping[str, object] | None) -> Mapping[str, object]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True)
class TurnRef:
    turn_id: str = field(repr=False)
    channel: Channel | str
    owner_id: str = field(repr=False)
    thread_ref: str = field(repr=False)
    nonce: str = field(default="", repr=False)

    @classmethod
    def create(
        cls,
        *,
        channel: Channel | str,
        owner_id: str,
        thread_ref: str,
    ) -> "TurnRef":
        return cls(
            turn_id=str(uuid.uuid4()),
            channel=channel,
            owner_id=owner_id,
            thread_ref=thread_ref,
        )

    def __post_init__(self) -> None:
        try:
            canonical_turn_id = str(uuid.UUID(str(self.turn_id)))
            channel = Channel(self.channel)
        except (TypeError, ValueError, AttributeError):
            raise ValueError("turn identity is invalid") from None
        expected_nonce = nonce_for_turn_id(canonical_turn_id)
        if self.nonce and self.nonce != expected_nonce:
            raise ValueError("turn nonce does not match turn identity")
        if (
            not isinstance(self.owner_id, str)
            or not _SAFE_IDENTIFIER.fullmatch(self.owner_id)
            or _SENSITIVE_MARKER.search(self.owner_id)
        ):
            raise ValueError("turn owner is invalid")
        if (
            not isinstance(self.thread_ref, str)
            or not _SAFE_IDENTIFIER.fullmatch(self.thread_ref)
            or _SENSITIVE_MARKER.search(self.thread_ref)
        ):
            raise ValueError("turn thread is invalid")
        object.__setattr__(self, "turn_id", canonical_turn_id)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "nonce", expected_nonce)


@dataclass(frozen=True)
class PreparedSend:
    turn: TurnRef
    text: str = field(repr=False)
    baseline: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "baseline", _freeze_mapping(self.baseline))


@dataclass(frozen=True)
class SendReceipt:
    turn: TurnRef
    phase: Phase
    code: str | None = None
    remote_ids: Mapping[str, object] = field(default_factory=dict, repr=False)
    baseline: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", Phase(self.phase))
        if self.code is not None:
            object.__setattr__(self, "code", normalize_code(self.code).value)
        object.__setattr__(self, "remote_ids", _freeze_mapping(self.remote_ids))
        object.__setattr__(self, "baseline", _freeze_mapping(self.baseline))


@dataclass(frozen=True)
class Observation:
    phase: Phase
    reply_text: str | None = field(default=None, repr=False)
    complete: bool = False
    complete_basis: str | None = None
    code: str | None = None
    next_poll_after_s: float = 0.0
    remote_ids: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", Phase(self.phase))
        object.__setattr__(self, "remote_ids", _freeze_mapping(self.remote_ids))
        if self.code is not None:
            object.__setattr__(self, "code", normalize_code(self.code).value)
        if self.complete_basis not in {
            None,
            "display_text_ready",
            "quiet_period",
            "journal_terminal",
            "provider_terminal",
        }:
            object.__setattr__(self, "complete_basis", None)
        try:
            poll_after = float(self.next_poll_after_s)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("next_poll_after_s must be nonnegative") from None
        if not math.isfinite(poll_after) or poll_after < 0:
            raise ValueError("next_poll_after_s must be nonnegative")
        object.__setattr__(self, "next_poll_after_s", poll_after)


@dataclass(frozen=True)
class LinkHealth:
    healthy: bool
    code: str | None = None
    stage: str = "health"
    http_status: int | None = None
    elapsed_ms: int = 0

    def __post_init__(self) -> None:
        if self.code is not None:
            object.__setattr__(self, "code", normalize_code(self.code).value)
        if self.stage not in {"prepare", "send", "observe", "health", "internal"}:
            object.__setattr__(self, "stage", "internal")
        if type(self.http_status) is not int or not 100 <= self.http_status <= 599:
            object.__setattr__(self, "http_status", None)
        try:
            elapsed = int(self.elapsed_ms)
        except (TypeError, ValueError, OverflowError):
            elapsed = 0
        object.__setattr__(self, "elapsed_ms", max(0, min(elapsed, 2_147_483_647)))


class AgentLink(Protocol):
    async def prepare(self, turn: TurnRef, text: str) -> PreparedSend: ...

    async def send(self, prepared: PreparedSend) -> SendReceipt: ...

    async def observe(
        self, turn: TurnRef, baseline: Mapping[str, object]
    ) -> Observation: ...

    async def health(self) -> LinkHealth: ...

    async def close(self) -> None: ...
