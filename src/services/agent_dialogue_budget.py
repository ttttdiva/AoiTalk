"""Finite budget and usage accounting for AgentDialogue.

The dialogue service owns admission and the transport/runtime owns execution.
This module keeps the shared accounting rules in one small, provider-neutral
boundary so a missing provider usage receipt cannot silently become zero.
Financial usage receipts are keyed by ``(budget_root_dialogue_id, run_id)``
when a chain root exists, and are safe to replay after a callback or process
restart.  A newly recorded receipt also updates the current child Dialogue's
local counters once.  Root-owned initial/source receipts are charged to the
root aggregate only; replaying that shared receipt from a successor child
never turns root overhead into child-local spend.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from ..memory.models import (
    AgentDialogue,
    AgentDialogueBudgetRoot,
    AgentDialogueDelivery,
    AgentDialogueReservation,
    AgentDialogueUsage,
    AgentRun,
    AgentWorkItem,
)

_TERMINAL_RUN_STATES = frozenset({"succeeded", "failed", "cancelled"})
LOCAL_USAGE_SCOPE = "new_receipt_for_current_dialogue"


MAX_DIALOGUE_BUDGET = {
    "max_turns": 100,
    "max_depth": 32,
    "max_attempts": 100,
    "max_tokens": 1_000_000,
    "max_cost_micros": 1_000_000_000_000,
}

DEFAULT_DIALOGUE_BUDGET = {
    "max_turns": 8,
    "max_depth": 4,
    "max_attempts": 16,
}

_BUDGET_ALIASES = {
    "turns": "max_turns",
    "max_turns": "max_turns",
    "depth": "max_depth",
    "max_depth": "max_depth",
    "attempts": "max_attempts",
    "max_attempts": "max_attempts",
    "tokens": "max_tokens",
    "max_tokens": "max_tokens",
    "cost": "max_cost_micros",
    "max_cost": "max_cost_micros",
    "cost_micros": "max_cost_micros",
    "max_cost_micros": "max_cost_micros",
    "currency": "cost_currency",
    "cost_currency": "cost_currency",
}

_USAGE_STATES = frozenset({"known", "unknown", "cost_unknown", "token_unknown"})


class DialogueBudgetError(ValueError):
    """Typed, safe budget validation/admission error."""

    def __init__(self, message: str, *, code: str = "dialogue_budget_invalid") -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class DialogueBudget:
    max_turns: int = DEFAULT_DIALOGUE_BUDGET["max_turns"]
    max_depth: int = DEFAULT_DIALOGUE_BUDGET["max_depth"]
    max_attempts: int = DEFAULT_DIALOGUE_BUDGET["max_attempts"]
    max_tokens: int | None = None
    max_cost_micros: int | None = None
    cost_currency: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_turns": self.max_turns,
            "max_depth": self.max_depth,
            "max_attempts": self.max_attempts,
            "max_tokens": self.max_tokens,
            "max_cost_micros": self.max_cost_micros,
            "cost_currency": self.cost_currency,
        }


@dataclass(frozen=True)
class DialogueUsageSnapshot:
    usage_key: str
    state: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    total_tokens: int | None = None
    cost_micros: int | None = None
    cost_currency: str | None = None
    reason: str | None = None

    @property
    def token_known(self) -> bool:
        return self.state in {"known", "cost_unknown"}

    @property
    def cost_known(self) -> bool:
        return self.state == "known" and self.cost_micros is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "usage_key": self.usage_key,
            "state": self.state,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "cost_micros": self.cost_micros,
            "cost_currency": self.cost_currency,
            "reason": self.reason,
        }


def _text(value: Any, limit: int = 255) -> str:
    return str(value or "").strip()[:limit]


def _safe_usage_digest(
    *,
    dialogue_id: UUID,
    run_id: UUID,
    snapshot: "DialogueUsageSnapshot",
) -> str:
    payload = {
        "dialogue_id": str(dialogue_id),
        "run_id": str(run_id),
        "usage_key": snapshot.usage_key,
        "state": snapshot.state,
        "input_tokens": snapshot.input_tokens,
        "output_tokens": snapshot.output_tokens,
        "cached_tokens": snapshot.cached_tokens,
        "total_tokens": snapshot.total_tokens,
        "cost_micros": snapshot.cost_micros,
        "cost_currency": snapshot.cost_currency,
        "reason": snapshot.reason,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _same_id(left: Any, right: Any) -> bool:
    if left in (None, "") or right in (None, ""):
        return left in (None, "") and right in (None, "")
    try:
        return UUID(str(left)) == UUID(str(right))
    except (TypeError, ValueError, AttributeError):
        return str(left) == str(right)


async def _locked_budget_owner(session: Any, dialogue: Any) -> Any:
    """Lock the chain aggregate and verify its server-derived owner scope."""

    root_id = getattr(dialogue, "budget_root_dialogue_id", None)
    if root_id in (None, ""):
        # Unit/rolling fixtures from before 0004 have no root table.  New
        # production rows are migrated and must never fall back silently.
        return dialogue
    root = (
        await session.execute(
            select(AgentDialogueBudgetRoot).where(AgentDialogueBudgetRoot.root_dialogue_id == root_id).with_for_update()
        )
    ).scalars().first()
    if root is None:
        raise DialogueBudgetError("Dialogue budget owner is missing", code="budget_owner_missing")
    if (
        not _same_id(root.owner_user_id, dialogue.owner_user_id)
        or not _same_id(root.root_session_id, dialogue.root_session_id)
        or not _same_id(root.root_agent_id, dialogue.root_agent_id)
        or not _same_id(root.root_agent_revision_id, dialogue.root_agent_revision_id)
    ):
        raise DialogueBudgetError("Dialogue budget owner scope changed", code="budget_owner_conflict")
    return root


def _has_separate_local_scope(dialogue: Any, budget_owner: Any) -> bool:
    """Whether local child counters differ from the shared financial owner."""

    root_id = getattr(budget_owner, "root_dialogue_id", None)
    return root_id not in (None, "") and not _same_id(root_id, getattr(dialogue, "id", None))


async def get_dialogue_budget_owner(
    session: Any,
    dialogue_or_id: Any,
    *,
    lock: bool = True,
) -> Any:
    """Return the server-derived root aggregate for a current Dialogue.

    The returned owner is a finance/budget scope; callers must still use the
    current child Dialogue/Run/Work identity for authority and transport ACLs.
    """

    if isinstance(dialogue_or_id, AgentDialogue):
        dialogue = dialogue_or_id
    else:
        dialogue_uuid = dialogue_or_id if isinstance(dialogue_or_id, UUID) else UUID(str(dialogue_or_id))
        statement = select(AgentDialogue).where(AgentDialogue.id == dialogue_uuid)
        if lock:
            statement = statement.with_for_update()
        dialogue = (await session.execute(statement)).scalars().first()
        if dialogue is None:
            raise DialogueBudgetError("dialogue not found", code="dialogue_not_found")
    if lock:
        return await _locked_budget_owner(session, dialogue)
    root_id = getattr(dialogue, "budget_root_dialogue_id", None)
    if root_id in (None, ""):
        return dialogue
    root = await session.get(AgentDialogueBudgetRoot, root_id)
    if root is None or not _same_id(root.owner_user_id, dialogue.owner_user_id) or not _same_id(root.root_session_id, dialogue.root_session_id) or not _same_id(root.root_agent_id, dialogue.root_agent_id) or not _same_id(root.root_agent_revision_id, dialogue.root_agent_revision_id):
        raise DialogueBudgetError("Dialogue budget owner scope changed", code="budget_owner_conflict")
    return root


def _integer(value: Any, *, field: str, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise DialogueBudgetError(f"{field} must be an integer", code="budget_invalid")
    try:
        integer = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DialogueBudgetError(f"{field} must be an integer", code="budget_invalid") from exc
    if isinstance(value, float) and (not math.isfinite(value) or value != integer):
        raise DialogueBudgetError(f"{field} must be an integer", code="budget_invalid")
    if integer < minimum or (maximum is not None and integer > maximum):
        raise DialogueBudgetError(f"{field} is outside its bounded range", code="budget_invalid")
    return integer


def _cost_micros(value: Any, *, field: str = "cost", minimum: int = 1) -> int:
    if isinstance(value, bool):
        raise DialogueBudgetError(f"{field} must be a finite amount", code="budget_invalid")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DialogueBudgetError(f"{field} must be a finite amount", code="budget_invalid") from exc
    if not amount.is_finite() or amount < 0:
        raise DialogueBudgetError(f"{field} must be a finite amount", code="budget_invalid")
    micros = int((amount * Decimal(1_000_000)).to_integral_value(rounding=ROUND_HALF_UP))
    if micros < minimum or micros > MAX_DIALOGUE_BUDGET["max_cost_micros"]:
        raise DialogueBudgetError(f"{field} is outside its bounded range", code="budget_invalid")
    return micros


def normalize_dialogue_budgets(value: Mapping[str, Any] | None) -> DialogueBudget:
    """Normalize a user budget and reject every unimplemented key.

    Earlier code silently ignored unknown budget keys, which made a caller
    believe that a token or cost ceiling was active when it was not.  The
    canonical result is intentionally explicit and serializable.
    """

    if value is None:
        return DialogueBudget()
    if not isinstance(value, Mapping):
        raise DialogueBudgetError("budgets must be an object", code="budget_invalid")

    normalized: dict[str, Any] = dict(DEFAULT_DIALOGUE_BUDGET)
    seen: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = _text(raw_key, 64).casefold()
        canonical = _BUDGET_ALIASES.get(key)
        if canonical is None:
            raise DialogueBudgetError(f"unsupported budget key: {key}", code="budget_key_invalid")
        if canonical in seen and seen[canonical] != raw_value:
            raise DialogueBudgetError(f"conflicting budget aliases: {canonical}", code="budget_conflict")
        seen[canonical] = raw_value

        if canonical == "max_turns":
            normalized[canonical] = _integer(raw_value, field=canonical, minimum=1, maximum=MAX_DIALOGUE_BUDGET[canonical])
        elif canonical == "max_depth":
            normalized[canonical] = _integer(raw_value, field=canonical, minimum=0, maximum=MAX_DIALOGUE_BUDGET[canonical])
        elif canonical == "max_attempts":
            normalized[canonical] = _integer(raw_value, field=canonical, minimum=1, maximum=MAX_DIALOGUE_BUDGET[canonical])
        elif canonical == "max_tokens":
            normalized[canonical] = _integer(raw_value, field=canonical, minimum=1, maximum=MAX_DIALOGUE_BUDGET[canonical])
        elif canonical == "max_cost_micros":
            normalized[canonical] = (
                _integer(raw_value, field=canonical, minimum=1, maximum=MAX_DIALOGUE_BUDGET[canonical])
                if key in {"cost_micros", "max_cost_micros"}
                else _cost_micros(raw_value)
            )
        elif canonical == "cost_currency":
            currency = _text(raw_value, 16).upper()
            if not currency or not currency.isalnum() or len(currency) not in {3, 4}:
                raise DialogueBudgetError("cost_currency is invalid", code="budget_invalid")
            normalized[canonical] = currency

    if normalized.get("max_cost_micros") is not None and not normalized.get("cost_currency"):
        normalized["cost_currency"] = "USD"
    return DialogueBudget(**normalized)


def budget_from_dialogue(value: Any) -> DialogueBudget:
    """Read a persisted Dialogue budget without trusting client metadata."""

    raw = {
        "max_turns": getattr(value, "max_turns", None),
        "max_depth": getattr(value, "max_depth", None),
        "max_attempts": getattr(value, "max_attempts", None),
        "max_tokens": getattr(value, "max_tokens", None),
        "max_cost_micros": getattr(value, "max_cost_micros", None),
        "cost_currency": getattr(value, "cost_currency", None),
    }
    return DialogueBudget(
        max_turns=_integer(raw["max_turns"] or DEFAULT_DIALOGUE_BUDGET["max_turns"], field="max_turns", minimum=1, maximum=MAX_DIALOGUE_BUDGET["max_turns"]),
        max_depth=_integer(raw["max_depth"] if raw["max_depth"] is not None else DEFAULT_DIALOGUE_BUDGET["max_depth"], field="max_depth", minimum=0, maximum=MAX_DIALOGUE_BUDGET["max_depth"]),
        max_attempts=_integer(raw["max_attempts"] or DEFAULT_DIALOGUE_BUDGET["max_attempts"], field="max_attempts", minimum=1, maximum=MAX_DIALOGUE_BUDGET["max_attempts"]),
        max_tokens=(
            _integer(raw["max_tokens"], field="max_tokens", minimum=1, maximum=MAX_DIALOGUE_BUDGET["max_tokens"])
            if raw["max_tokens"] is not None
            else None
        ),
        max_cost_micros=(
            _integer(raw["max_cost_micros"], field="max_cost_micros", minimum=1, maximum=MAX_DIALOGUE_BUDGET["max_cost_micros"])
            if raw["max_cost_micros"] is not None
            else None
        ),
        cost_currency=_text(raw["cost_currency"], 16).upper() or None,
    )


def _usage_value(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if value.get(key) is not None:
            return value.get(key)
    return None


def _rate_cost(tokens: int, rate: Any) -> int:
    try:
        amount = Decimal(str(rate)) * Decimal(tokens) / Decimal(1000)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DialogueBudgetError("rate card is invalid", code="usage_invalid") from exc
    if not amount.is_finite() or amount < 0:
        raise DialogueBudgetError("rate card is invalid", code="usage_invalid")
    return int((amount * Decimal(1_000_000)).to_integral_value(rounding=ROUND_HALF_UP))


def normalize_dialogue_usage(
    value: Mapping[str, Any] | None,
    *,
    usage_key: str | None = None,
    rate_card: Mapping[str, Any] | None = None,
) -> DialogueUsageSnapshot:
    """Normalize one provider usage payload without guessing missing tokens."""

    key = _text(usage_key or (value.get("usage_key") if isinstance(value, Mapping) else None), 255)
    if not key:
        raise DialogueBudgetError("usage_key is required", code="usage_key_required")
    if not isinstance(value, Mapping):
        return DialogueUsageSnapshot(key, "unknown", reason="usage_missing")

    try:
        input_raw = _usage_value(value, "input_tokens", "prompt_tokens")
        output_raw = _usage_value(value, "output_tokens", "completion_tokens")
        cached_raw = _usage_value(value, "cached_tokens", "cache_read_input_tokens")
        total_raw = _usage_value(value, "total_tokens", "tokens")
        reasoning_raw = _usage_value(value, "reasoning_tokens")
        input_tokens = _integer(input_raw, field="input_tokens") if input_raw is not None else None
        output_tokens = _integer(output_raw, field="output_tokens") if output_raw is not None else None
        cached_tokens = _integer(cached_raw, field="cached_tokens") if cached_raw is not None else 0
        total_tokens = _integer(total_raw, field="total_tokens") if total_raw is not None else None
        reasoning_tokens = _integer(reasoning_raw, field="reasoning_tokens") if reasoning_raw is not None else 0
    except DialogueBudgetError as exc:
        return DialogueUsageSnapshot(key, "unknown", reason=exc.code)

    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        output_includes_reasoning = value.get("output_includes_reasoning") is True
        total_tokens = input_tokens + output_tokens + (
            0
            if output_includes_reasoning
            else reasoning_tokens
        )
    if total_tokens is None:
        return DialogueUsageSnapshot(key, "token_unknown", input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens, reason="token_usage_missing")
    if input_tokens is not None and output_tokens is not None and total_tokens < max(0, input_tokens + output_tokens - cached_tokens):
        return DialogueUsageSnapshot(key, "unknown", reason="token_usage_inconsistent")

    currency = _text(_usage_value(value, "cost_currency", "currency"), 16).upper() or None
    reported_cost = _usage_value(value, "cost_micros")
    if reported_cost is not None:
        try:
            cost_micros = _integer(reported_cost, field="cost_micros", minimum=0, maximum=MAX_DIALOGUE_BUDGET["max_cost_micros"])
        except DialogueBudgetError as exc:
            return DialogueUsageSnapshot(key, "unknown", input_tokens, output_tokens, cached_tokens, total_tokens, reason=exc.code)
    else:
        reported_amount = _usage_value(value, "cost", "cost_usd", "price")
        cost_micros = None
        if reported_amount is not None:
            try:
                cost_micros = _cost_micros(reported_amount, field="cost", minimum=0)
            except DialogueBudgetError as exc:
                return DialogueUsageSnapshot(key, "unknown", input_tokens, output_tokens, cached_tokens, total_tokens, reason=exc.code)
        elif isinstance(rate_card, Mapping):
            input_rate = _usage_value(rate_card, "input_per_1k", "input_cost_per_1k")
            output_rate = _usage_value(rate_card, "output_per_1k", "output_cost_per_1k")
            cached_rate = _usage_value(rate_card, "cached_per_1k", "cached_cost_per_1k")
            if input_rate is not None and output_rate is not None and input_tokens is not None and output_tokens is not None:
                cost_micros = _rate_cost(input_tokens, input_rate) + _rate_cost(output_tokens, output_rate)
                if cached_rate is not None and cached_tokens:
                    cost_micros += _rate_cost(cached_tokens, cached_rate)
                currency = currency or _text(rate_card.get("currency"), 16).upper() or None

    if cost_micros is None:
        return DialogueUsageSnapshot(key, "cost_unknown", input_tokens, output_tokens, cached_tokens, total_tokens, None, currency, "cost_unavailable")
    if not currency:
        return DialogueUsageSnapshot(key, "cost_unknown", input_tokens, output_tokens, cached_tokens, total_tokens, None, None, "cost_currency_missing")
    return DialogueUsageSnapshot(key, "known", input_tokens, output_tokens, cached_tokens, total_tokens, cost_micros, currency)


def assert_dialogue_budget_available(
    value: Any,
    *,
    turns: int = 1,
    attempts: int = 0,
    tokens: int = 0,
    cost_micros: int = 0,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Fail closed before creating a new Run/Work/Delivery."""

    budget = budget_from_dialogue(value) if not isinstance(value, DialogueBudget) else value
    turns = _integer(turns, field="turns", minimum=0)
    attempts = _integer(attempts, field="attempts", minimum=0)
    if input_tokens is not None or output_tokens is not None:
        tokens = int(input_tokens or 0) + int(output_tokens or 0)
    tokens = _integer(tokens, field="tokens", minimum=0)
    cost_micros = _integer(cost_micros, field="cost_micros", minimum=0)
    if int(getattr(value, "turn_count", 0) or 0) + turns > budget.max_turns:
        raise DialogueBudgetError("dialogue turn budget exhausted", code="dialogue_turn_budget_exhausted")
    if int(getattr(value, "attempt_count", 0) or 0) + attempts > budget.max_attempts:
        raise DialogueBudgetError("dialogue attempt budget exhausted", code="dialogue_attempt_budget_exhausted")
    usage_state = _text(getattr(value, "usage_state", "known"), 24) or "known"
    if budget.max_tokens is not None:
        if usage_state in {"unknown", "token_unknown"}:
            raise DialogueBudgetError("token usage is unknown", code="dialogue_token_usage_unknown")
        consumed_tokens = int(getattr(value, "total_tokens", 0) or 0)
        reserved_tokens = int(getattr(value, "reserved_tokens", 0) or 0)
        if consumed_tokens >= budget.max_tokens or consumed_tokens + reserved_tokens + tokens > budget.max_tokens:
            raise DialogueBudgetError("dialogue token budget exhausted", code="dialogue_token_budget_exhausted")
    if budget.max_cost_micros is not None:
        if usage_state in {"unknown", "cost_unknown", "token_unknown"}:
            raise DialogueBudgetError("cost usage is unknown", code="dialogue_cost_usage_unknown")
        consumed_cost = int(getattr(value, "cost_micros", 0) or 0)
        reserved_cost = int(getattr(value, "reserved_cost_micros", 0) or 0)
        if consumed_cost >= budget.max_cost_micros or consumed_cost + reserved_cost + cost_micros > budget.max_cost_micros:
            raise DialogueBudgetError("dialogue cost budget exhausted", code="dialogue_cost_budget_exhausted")


async def reserve_dialogue_budget(
    session: Any,
    dialogue_id: Any,
    *,
    turns: int = 1,
    attempts: int = 0,
    tokens: int = 0,
    cost_micros: int = 0,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    reservation_key: str | None = None,
    run_id: Any | None = None,
) -> Any:
    """Lock, validate, and reserve a finite admission budget atomically.

    Callers must provide a stable key for every non-empty reservation so
    retries converge on the same row instead of consuming another turn.
    """

    try:
        dialogue_uuid = dialogue_id if isinstance(dialogue_id, UUID) else UUID(str(dialogue_id))
    except (TypeError, ValueError) as exc:
        raise DialogueBudgetError("dialogue_id is invalid", code="dialogue_id_invalid") from exc
    result = await session.execute(
        select(AgentDialogue).where(AgentDialogue.id == dialogue_uuid).with_for_update()
    )
    dialogue = result.scalars().first()
    if dialogue is None:
        raise DialogueBudgetError("dialogue not found", code="dialogue_not_found")
    budget_owner = await _locked_budget_owner(session, dialogue)
    budget_root_id = getattr(budget_owner, "root_dialogue_id", None)
    local_separate = _has_separate_local_scope(dialogue, budget_owner)
    if input_tokens is not None or output_tokens is not None:
        tokens = int(input_tokens or 0) + int(output_tokens or 0)
    tokens = _integer(tokens, field="tokens", minimum=0)
    cost_micros = _integer(cost_micros, field="cost_micros", minimum=0)
    key = _text(reservation_key, 255)
    if (tokens or cost_micros or turns or attempts) and not key:
        raise DialogueBudgetError("reservation_key is required", code="reservation_key_required")
    reservation_uuid = None
    if run_id is not None:
        try:
            reservation_uuid = run_id if isinstance(run_id, UUID) else UUID(str(run_id))
        except (TypeError, ValueError) as exc:
            raise DialogueBudgetError("run_id is invalid", code="reservation_run_invalid") from exc
    if key:
        existing = (
            await session.execute(
                select(AgentDialogueReservation).where(
                    AgentDialogueReservation.budget_root_dialogue_id == budget_root_id
                    if budget_root_id is not None
                    else AgentDialogueReservation.dialogue_id == dialogue_uuid,
                    AgentDialogueReservation.reservation_key == key,
                )
            )
        ).scalars().first()
        if existing is not None:
            # A reservation belongs to the current child execution as well as
            # the shared root ledger.  Reusing a root key for another child
            # would make that child's local reservation invisible and would
            # let its local ceiling be bypassed.
            if not _same_id(existing.dialogue_id, dialogue_uuid):
                raise DialogueBudgetError("reservation key belongs to another Dialogue", code="reservation_conflict")
            if (
                int(existing.reserved_tokens or 0) != tokens
                or int(existing.reserved_cost_micros or 0) != cost_micros
                or not _same_id(existing.run_id, reservation_uuid)
            ):
                raise DialogueBudgetError("reservation payload changed", code="reservation_conflict")
            return dialogue
    assert_dialogue_budget_available(budget_owner, turns=turns, attempts=attempts, tokens=tokens, cost_micros=cost_micros)
    if local_separate:
        assert_dialogue_budget_available(dialogue, turns=turns, attempts=attempts, tokens=tokens, cost_micros=cost_micros)
    budget_owner.turn_count = int(budget_owner.turn_count or 0) + int(turns)
    budget_owner.attempt_count = int(budget_owner.attempt_count or 0) + int(attempts)
    if tokens:
        budget_owner.reserved_tokens = int(budget_owner.reserved_tokens or 0) + tokens
    if cost_micros:
        budget_owner.reserved_cost_micros = int(budget_owner.reserved_cost_micros or 0) + cost_micros
    if local_separate:
        dialogue.turn_count = int(dialogue.turn_count or 0) + int(turns)
        dialogue.attempt_count = int(dialogue.attempt_count or 0) + int(attempts)
        if tokens:
            dialogue.reserved_tokens = int(dialogue.reserved_tokens or 0) + tokens
        if cost_micros:
            dialogue.reserved_cost_micros = int(dialogue.reserved_cost_micros or 0) + cost_micros
    if key:
        session.add(
            AgentDialogueReservation(
                dialogue_id=dialogue_uuid,
                budget_root_dialogue_id=budget_root_id,
                run_id=reservation_uuid,
                reservation_key=key,
                state="reserved",
                reserved_tokens=tokens,
                reserved_cost_micros=cost_micros,
            )
        )
    await session.flush()
    return dialogue


async def release_dialogue_budget(
    session: Any,
    dialogue_id: Any,
    reservation_key: str,
    *,
    expected_dialogue_id: Any | None = None,
    expected_run_id: Any | None = None,
) -> dict[str, Any]:
    """Release an in-flight reservation exactly once."""

    dialogue_uuid = dialogue_id if isinstance(dialogue_id, UUID) else UUID(str(dialogue_id))
    dialogue = (
        await session.execute(select(AgentDialogue).where(AgentDialogue.id == dialogue_uuid).with_for_update())
    ).scalars().first()
    if dialogue is None:
        raise DialogueBudgetError("dialogue not found", code="dialogue_not_found")
    budget_owner = await _locked_budget_owner(session, dialogue)
    budget_root_id = getattr(budget_owner, "root_dialogue_id", None)
    local_separate = _has_separate_local_scope(dialogue, budget_owner)
    reservation_filters = [
        AgentDialogueReservation.reservation_key == _text(reservation_key, 255),
    ]
    if expected_dialogue_id is not None:
        expected_dialogue_uuid = (
            expected_dialogue_id
            if isinstance(expected_dialogue_id, UUID)
            else UUID(str(expected_dialogue_id))
        )
        reservation_filters.append(
            AgentDialogueReservation.dialogue_id == expected_dialogue_uuid
        )
    elif budget_root_id is None:
        reservation_filters.append(
            AgentDialogueReservation.dialogue_id == dialogue_uuid
        )
    if expected_run_id is not None:
        expected_run_uuid = (
            expected_run_id
            if isinstance(expected_run_id, UUID)
            else UUID(str(expected_run_id))
        )
        reservation_filters.append(
            AgentDialogueReservation.run_id == expected_run_uuid
        )
    reservation = (
        await session.execute(
            select(AgentDialogueReservation)
            .where(
                *(
                    [
                        AgentDialogueReservation.budget_root_dialogue_id
                        == budget_root_id
                    ]
                    if budget_root_id is not None
                    else []
                ),
                *reservation_filters,
            )
            .with_for_update()
        )
    ).scalars().first()
    if reservation is None:
        raise DialogueBudgetError("reservation not found", code="reservation_not_found")
    was_reserved = reservation.state == "reserved"
    if was_reserved:
        budget_owner.reserved_tokens = max(0, int(budget_owner.reserved_tokens or 0) - int(reservation.reserved_tokens or 0))
        budget_owner.reserved_cost_micros = max(0, int(budget_owner.reserved_cost_micros or 0) - int(reservation.reserved_cost_micros or 0))
        if local_separate:
            dialogue.reserved_tokens = max(0, int(dialogue.reserved_tokens or 0) - int(reservation.reserved_tokens or 0))
            dialogue.reserved_cost_micros = max(0, int(dialogue.reserved_cost_micros or 0) - int(reservation.reserved_cost_micros or 0))
        reservation.state = "released"
        reservation.released_at = datetime.utcnow()
        await session.flush()
    return {"reservation_id": str(reservation.id), "state": reservation.state, "idempotent": not was_reserved}


async def charge_dialogue_budget(
    session: Any,
    dialogue_id: Any,
    reservation_key: str,
    *,
    tokens: int = 0,
    cost_micros: int = 0,
) -> dict[str, Any]:
    """Charge a reservation once and release its remaining in-flight capacity."""

    dialogue_uuid = dialogue_id if isinstance(dialogue_id, UUID) else UUID(str(dialogue_id))
    tokens = _integer(tokens, field="tokens", minimum=0)
    cost_micros = _integer(cost_micros, field="cost_micros", minimum=0)
    dialogue = (
        await session.execute(select(AgentDialogue).where(AgentDialogue.id == dialogue_uuid).with_for_update())
    ).scalars().first()
    if dialogue is None:
        raise DialogueBudgetError("dialogue not found", code="dialogue_not_found")
    budget_owner = await _locked_budget_owner(session, dialogue)
    budget_root_id = getattr(budget_owner, "root_dialogue_id", None)
    local_separate = _has_separate_local_scope(dialogue, budget_owner)
    reservation = (
        await session.execute(
            select(AgentDialogueReservation)
            .where(
                AgentDialogueReservation.budget_root_dialogue_id == budget_root_id
                if budget_root_id is not None
                else AgentDialogueReservation.dialogue_id == dialogue_uuid,
                AgentDialogueReservation.reservation_key == _text(reservation_key, 255),
            )
            .with_for_update()
        )
    ).scalars().first()
    if reservation is None:
        raise DialogueBudgetError("reservation not found", code="reservation_not_found")
    if reservation.state in {"charged", "overrun"}:
        return {"reservation_id": str(reservation.id), "state": reservation.state, "idempotent": True, "overrun": reservation.state == "overrun"}
    if reservation.state == "released":
        if tokens == 0 and cost_micros == 0:
            return {"reservation_id": str(reservation.id), "state": "released", "idempotent": True, "skip_charge": True}
        overrun = False
        if budget_owner.max_tokens is not None and int(budget_owner.total_tokens or 0) + tokens > int(budget_owner.max_tokens):
            overrun = True
        if budget_owner.max_cost_micros is not None and int(budget_owner.cost_micros or 0) + cost_micros > int(budget_owner.max_cost_micros):
            overrun = True
        reservation.charged_tokens = tokens
        reservation.charged_cost_micros = cost_micros
        reservation.state = "overrun" if overrun else "charged"
        reservation.released_at = datetime.utcnow()
        if overrun:
            budget_owner.usage_state = "unknown"
            if local_separate:
                dialogue.usage_state = "unknown"
        await session.flush()
        return {"reservation_id": str(reservation.id), "state": reservation.state, "idempotent": False, "finalized_after_release": True, "overrun": overrun}
    if reservation.state != "reserved":
        raise DialogueBudgetError("reservation is not chargeable", code="reservation_state_conflict")
    overrun = tokens > int(reservation.reserved_tokens or 0) or cost_micros > int(reservation.reserved_cost_micros or 0)
    budget_owner.reserved_tokens = max(0, int(budget_owner.reserved_tokens or 0) - int(reservation.reserved_tokens or 0))
    budget_owner.reserved_cost_micros = max(0, int(budget_owner.reserved_cost_micros or 0) - int(reservation.reserved_cost_micros or 0))
    if local_separate:
        dialogue.reserved_tokens = max(0, int(dialogue.reserved_tokens or 0) - int(reservation.reserved_tokens or 0))
        dialogue.reserved_cost_micros = max(0, int(dialogue.reserved_cost_micros or 0) - int(reservation.reserved_cost_micros or 0))
    reservation.charged_tokens = tokens
    reservation.charged_cost_micros = cost_micros
    reservation.state = "overrun" if overrun else "charged"
    reservation.released_at = datetime.utcnow()
    if overrun:
        # The provider already ran. Preserve the fact and fence all later
        # admissions; never roll back the receipt and blindly replay it.
        budget_owner.usage_state = "unknown"
        if local_separate:
            dialogue.usage_state = "unknown"
    await session.flush()
    return {"reservation_id": str(reservation.id), "state": reservation.state, "idempotent": False, "overrun": overrun}


def _merge_usage_state(current: str, incoming: str) -> str:
    priority = {"known": 0, "cost_unknown": 1, "token_unknown": 2, "unknown": 3}
    current = current if current in _USAGE_STATES else "unknown"
    incoming = incoming if incoming in _USAGE_STATES else "unknown"
    return current if priority[current] >= priority[incoming] else incoming


async def _validate_usage_identity(
    session: Any,
    dialogue: Any,
    run_uuid: UUID,
    *,
    role: str | None = None,
) -> tuple[Any, Any | None]:
    """Bind a usage receipt to one terminal root/worker Run and its evidence."""

    run = await session.get(AgentRun, run_uuid)
    if run is None or str(run.status or "").casefold() not in _TERMINAL_RUN_STATES or run.parent_run_id is not None:
        raise DialogueBudgetError("usage Run is not a canonical terminal Run", code="usage_run_identity_conflict")
    normalized_role = _text(role, 16).casefold() or None
    if normalized_role not in {None, "root", "worker"}:
        raise DialogueBudgetError("usage role is invalid", code="usage_role_invalid")
    if normalized_role is None:
        normalized_role = "worker" if _same_id(run.session_id, dialogue.worker_session_id) else "root" if _same_id(run.session_id, dialogue.root_session_id) else None
    if normalized_role is None:
        raise DialogueBudgetError("usage Run session is not bound to Dialogue", code="usage_run_identity_conflict")
    expected_session = dialogue.worker_session_id if normalized_role == "worker" else dialogue.root_session_id
    expected_agent = dialogue.worker_agent_id if normalized_role == "worker" else dialogue.root_agent_id
    expected_revision = dialogue.worker_agent_revision_id if normalized_role == "worker" else dialogue.root_agent_revision_id
    if (
        not _same_id(run.session_id, expected_session)
        or not _same_id(run.agent_id, expected_agent)
        or not _same_id(run.agent_revision_id, expected_revision)
        or str(run.user_id or "") != str(dialogue.owner_user_id)
        or run.work_item_id is None
    ):
        raise DialogueBudgetError("usage Run Agent/session/revision conflict", code="usage_run_identity_conflict")
    work = await session.get(AgentWorkItem, run.work_item_id)
    if (
        work is None
        or not _same_id(work.id, run.work_item_id)
        or not _same_id(work.conversation_session_id, expected_session)
        or not _same_id(work.assigned_agent_id, expected_agent)
        or not _same_id(work.agent_revision_id, expected_revision)
    ):
        raise DialogueBudgetError("usage Work binding is not canonical", code="usage_work_identity_conflict")
    delivery = (
        await session.execute(
            select(AgentDialogueDelivery)
            .where(
                AgentDialogueDelivery.dialogue_id == dialogue.id,
                AgentDialogueDelivery.receiver_run_id == run_uuid,
            )
            .order_by(AgentDialogueDelivery.sequence.desc())
            .limit(1)
        )
    ).scalars().first()
    if normalized_role == "root" and delivery is None:
        metadata = run.run_metadata if isinstance(run.run_metadata, Mapping) else {}
        marker = metadata.get("agent_dialogue") if isinstance(metadata.get("agent_dialogue"), Mapping) else {}
        marker_revision = marker.get("control_revision")
        if marker_revision is not None:
            try:
                parsed_marker_revision = int(marker_revision)
            except (TypeError, ValueError) as exc:
                raise DialogueBudgetError("usage root control revision is invalid", code="usage_control_revision_conflict") from exc
            if parsed_marker_revision != int(dialogue.control_revision or 0):
                raise DialogueBudgetError("usage root control revision is stale", code="usage_control_revision_conflict")
        if (
            _same_id(run.id, dialogue.human_run_id)
            or (_text(marker.get("dialogue_id"), 128) == str(dialogue.id) and _text(marker.get("role"), 16).casefold() == "root")
        ):
            if dialogue.human_work_item_id is not None and not _same_id(run.work_item_id, dialogue.human_work_item_id) and _same_id(run.id, dialogue.human_run_id):
                raise DialogueBudgetError("usage source Work references conflict", code="usage_source_work_identity_conflict")
            return run, None
        raise DialogueBudgetError("usage root Delivery is missing", code="usage_delivery_missing")
    if delivery is None:
        raise DialogueBudgetError("usage Delivery is missing", code="usage_delivery_missing")
    if (
        not _same_id(delivery.receiver_session_id, run.session_id)
        or not _same_id(delivery.receiver_agent_id, run.agent_id)
        or not _same_id(delivery.receiver_agent_revision_id, run.agent_revision_id)
        or not _same_id(delivery.receiver_work_item_id, run.work_item_id)
    ):
        raise DialogueBudgetError("usage receiver references conflict", code="usage_receiver_identity_conflict")
    expected_kinds = {"request", "control"} if normalized_role == "worker" else {"response", "event"}
    if delivery.kind not in expected_kinds:
        raise DialogueBudgetError("usage Delivery direction conflicts", code="usage_delivery_direction_conflict")
    source_run = await session.get(AgentRun, delivery.source_run_id) if delivery.source_run_id else None
    source_agent = dialogue.root_agent_id if normalized_role == "worker" else dialogue.worker_agent_id
    source_revision = dialogue.root_agent_revision_id if normalized_role == "worker" else dialogue.worker_agent_revision_id
    source_session = dialogue.root_session_id if normalized_role == "worker" else dialogue.worker_session_id
    if (
        source_run is None
        or not _same_id(source_run.session_id, source_session)
        or not _same_id(source_run.agent_id, source_agent)
        or not _same_id(source_run.agent_revision_id, source_revision)
        or str(source_run.user_id or "") != str(dialogue.owner_user_id)
        or source_run.parent_run_id is not None
    ):
        raise DialogueBudgetError("usage source references conflict", code="usage_source_identity_conflict")
    if delivery.source_work_item_id is not None:
        source_work = await session.get(AgentWorkItem, delivery.source_work_item_id)
        if source_work is None or not _same_id(source_work.conversation_session_id, source_session) or not _same_id(source_work.assigned_agent_id, source_agent) or not _same_id(source_work.agent_revision_id, source_revision):
            raise DialogueBudgetError("usage source Work references conflict", code="usage_source_work_identity_conflict")
    return run, delivery


async def record_dialogue_usage(
    session: Any,
    *,
    dialogue_id: Any,
    run_id: Any,
    usage: Mapping[str, Any] | None,
    usage_key: str | None = None,
    rate_card: Mapping[str, Any] | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """Record one exact root/worker Run receipt and update aggregates once.

    ``role='worker'`` is the default for child provider Runs.  Root initial,
    control, and receipt Runs must pass ``role='root'`` so the identity fence
    does not mistake them for worker executions.
    """

    try:
        dialogue_uuid = dialogue_id if isinstance(dialogue_id, UUID) else UUID(str(dialogue_id))
        run_uuid = run_id if isinstance(run_id, UUID) else UUID(str(run_id))
    except (TypeError, ValueError) as exc:
        raise DialogueBudgetError("usage identity is invalid", code="usage_identity_invalid") from exc
    key = _text(usage_key or f"run:{run_uuid}", 255)
    snapshot = normalize_dialogue_usage(usage, usage_key=key, rate_card=rate_card)
    row = (
        await session.execute(select(AgentDialogue).where(AgentDialogue.id == dialogue_uuid).with_for_update())
    ).scalars().first()
    if row is None:
        raise DialogueBudgetError("dialogue not found", code="dialogue_not_found")
    budget_owner = await _locked_budget_owner(session, row)
    budget_root_id = getattr(budget_owner, "root_dialogue_id", None)
    validated_run, _ = await _validate_usage_identity(session, row, run_uuid, role=role)
    effective_role = _text(role, 16).casefold() or (
        "worker" if _same_id(validated_run.session_id, row.worker_session_id) else "root"
    )
    # Root initial/source receipts are shared chain overhead.  Only a newly
    # recorded worker receipt belongs to the current child local ceiling.
    local_separate = _has_separate_local_scope(row, budget_owner) and effective_role == "worker"
    # Financial accounting is scoped to the validated root aggregate.  A
    # successor/child Dialogue may carry the same canonical Run/source usage;
    # using the child id here would turn that root/run replay into a false
    # digest conflict even though the current child identity was valid.
    receipt_scope_id = budget_root_id or dialogue_uuid
    usage_digest = _safe_usage_digest(dialogue_id=receipt_scope_id, run_id=run_uuid, snapshot=snapshot)

    def actual_snapshot(receipt: Any, *, fallback: DialogueUsageSnapshot | None = None) -> dict[str, Any]:
        value = fallback or DialogueUsageSnapshot(
            usage_key=str(receipt.usage_key),
            state=str(receipt.state),
            input_tokens=receipt.input_tokens,
            output_tokens=receipt.output_tokens,
            cached_tokens=receipt.cached_tokens,
            total_tokens=receipt.total_tokens,
            cost_micros=receipt.cost_micros,
            cost_currency=receipt.cost_currency,
            reason=receipt.reason,
        )
        return value.to_dict()
    existing = (
        await session.execute(
            select(AgentDialogueUsage).where(
                AgentDialogueUsage.budget_root_dialogue_id == budget_root_id
                if budget_root_id is not None
                else AgentDialogueUsage.dialogue_id == dialogue_uuid,
                AgentDialogueUsage.run_id == run_uuid,
            )
        )
    ).scalars().first()
    if existing is not None:
        if existing.usage_key != key:
            raise DialogueBudgetError("usage key changed for Run", code="usage_receipt_conflict")
        if existing.usage_digest == usage_digest:
            actual = actual_snapshot(existing)
            return {"id": str(existing.id), "idempotent": True, "state": existing.state, "usage": actual, "actual_usage": actual}
        existing_token_known = existing.state in {"known", "cost_unknown"}
        incoming_token_known = snapshot.token_known
        existing_cost_known = existing.state == "known" and existing.cost_micros is not None
        incoming_cost_known = snapshot.cost_known
        can_upgrade = existing.state in {"unknown", "token_unknown", "cost_unknown"} and (
            snapshot.state == "known"
            or (existing.state in {"unknown", "token_unknown"} and snapshot.state == "cost_unknown")
        )
        if not can_upgrade:
            raise DialogueBudgetError("usage receipt payload changed", code="usage_receipt_conflict")
        if existing_token_known and incoming_token_known and (
            int(existing.total_tokens or 0) != int(snapshot.total_tokens or 0)
            or int(existing.input_tokens or 0) != int(snapshot.input_tokens or 0)
            or int(existing.output_tokens or 0) != int(snapshot.output_tokens or 0)
            or int(existing.cached_tokens or 0) != int(snapshot.cached_tokens or 0)
        ):
            raise DialogueBudgetError("usage token payload changed", code="usage_receipt_conflict")
        if existing_cost_known and incoming_cost_known and (
            int(existing.cost_micros or 0) != int(snapshot.cost_micros or 0)
            or existing.cost_currency != snapshot.cost_currency
        ):
            raise DialogueBudgetError("usage cost payload changed", code="usage_receipt_conflict")
        if incoming_token_known and not existing_token_known:
            budget_owner.input_tokens = int(budget_owner.input_tokens or 0) + int(snapshot.input_tokens or 0)
            budget_owner.output_tokens = int(budget_owner.output_tokens or 0) + int(snapshot.output_tokens or 0)
            budget_owner.cached_tokens = int(budget_owner.cached_tokens or 0) + int(snapshot.cached_tokens or 0)
            budget_owner.total_tokens = int(budget_owner.total_tokens or 0) + int(snapshot.total_tokens or 0)
            if local_separate:
                row.input_tokens = int(row.input_tokens or 0) + int(snapshot.input_tokens or 0)
                row.output_tokens = int(row.output_tokens or 0) + int(snapshot.output_tokens or 0)
                row.cached_tokens = int(row.cached_tokens or 0) + int(snapshot.cached_tokens or 0)
                row.total_tokens = int(row.total_tokens or 0) + int(snapshot.total_tokens or 0)
        if incoming_cost_known and not existing_cost_known:
            if budget_owner.cost_currency not in (None, snapshot.cost_currency):
                raise DialogueBudgetError("usage currency changed", code="usage_currency_conflict")
            budget_owner.cost_currency = budget_owner.cost_currency or snapshot.cost_currency
            budget_owner.cost_micros = int(budget_owner.cost_micros or 0) + int(snapshot.cost_micros or 0)
            if local_separate:
                if row.cost_currency not in (None, snapshot.cost_currency):
                    raise DialogueBudgetError("usage currency changed", code="usage_currency_conflict")
                row.cost_currency = row.cost_currency or snapshot.cost_currency
                row.cost_micros = int(row.cost_micros or 0) + int(snapshot.cost_micros or 0)
        existing.state = snapshot.state
        existing.input_tokens = snapshot.input_tokens
        existing.output_tokens = snapshot.output_tokens
        existing.cached_tokens = snapshot.cached_tokens
        existing.total_tokens = snapshot.total_tokens
        existing.cost_micros = snapshot.cost_micros
        existing.cost_currency = snapshot.cost_currency
        existing.reason = snapshot.reason
        existing.usage_digest = usage_digest
        states = (
            await session.execute(
                select(AgentDialogueUsage.state).where(
                    AgentDialogueUsage.budget_root_dialogue_id == budget_root_id
                    if budget_root_id is not None
                    else AgentDialogueUsage.dialogue_id == dialogue_uuid
                )
            )
        ).scalars().all()
        merged_state = "known"
        for state in states:
            merged_state = _merge_usage_state(merged_state, str(state))
        budget_owner.usage_state = merged_state
        if local_separate:
            local_states = (
                await session.execute(
                    select(AgentDialogueUsage.state).where(AgentDialogueUsage.dialogue_id == dialogue_uuid)
                )
            ).scalars().all()
            local_merged_state = "known"
            for state in local_states:
                local_merged_state = _merge_usage_state(local_merged_state, str(state))
            row.usage_state = local_merged_state
        await session.flush()
        actual = actual_snapshot(existing)
        return {"id": str(existing.id), "idempotent": False, "upgraded": True, "state": existing.state, "usage": actual, "actual_usage": actual}
    receipt = AgentDialogueUsage(
        dialogue_id=dialogue_uuid,
        budget_root_dialogue_id=budget_root_id,
        run_id=run_uuid,
        usage_key=key,
        state=snapshot.state,
        input_tokens=snapshot.input_tokens,
        output_tokens=snapshot.output_tokens,
        cached_tokens=snapshot.cached_tokens,
        total_tokens=snapshot.total_tokens,
        cost_micros=snapshot.cost_micros,
        cost_currency=snapshot.cost_currency,
        reason=snapshot.reason,
        usage_digest=usage_digest,
    )
    session.add(receipt)
    await session.flush()
    budget_owner.usage_state = _merge_usage_state(_text(budget_owner.usage_state, 24) or "known", snapshot.state)
    if local_separate:
        row.usage_state = _merge_usage_state(_text(row.usage_state, 24) or "known", snapshot.state)
    if snapshot.token_known:
        budget_owner.input_tokens = int(budget_owner.input_tokens or 0) + int(snapshot.input_tokens or 0)
        budget_owner.output_tokens = int(budget_owner.output_tokens or 0) + int(snapshot.output_tokens or 0)
        budget_owner.cached_tokens = int(budget_owner.cached_tokens or 0) + int(snapshot.cached_tokens or 0)
        budget_owner.total_tokens = int(budget_owner.total_tokens or 0) + int(snapshot.total_tokens or 0)
        if local_separate:
            row.input_tokens = int(row.input_tokens or 0) + int(snapshot.input_tokens or 0)
            row.output_tokens = int(row.output_tokens or 0) + int(snapshot.output_tokens or 0)
            row.cached_tokens = int(row.cached_tokens or 0) + int(snapshot.cached_tokens or 0)
            row.total_tokens = int(row.total_tokens or 0) + int(snapshot.total_tokens or 0)
    if snapshot.cost_known:
        if budget_owner.cost_currency not in (None, snapshot.cost_currency):
            raise DialogueBudgetError("usage currency changed", code="usage_currency_conflict")
        budget_owner.cost_currency = budget_owner.cost_currency or snapshot.cost_currency
        budget_owner.cost_micros = int(budget_owner.cost_micros or 0) + int(snapshot.cost_micros or 0)
        if local_separate:
            if row.cost_currency not in (None, snapshot.cost_currency):
                raise DialogueBudgetError("usage currency changed", code="usage_currency_conflict")
            row.cost_currency = row.cost_currency or snapshot.cost_currency
            row.cost_micros = int(row.cost_micros or 0) + int(snapshot.cost_micros or 0)
    return {"id": str(receipt.id), "idempotent": False, "state": snapshot.state, "usage": snapshot.to_dict(), "actual_usage": snapshot.to_dict()}


def project_dialogue_budget(value: Any, *, budget_owner: Any | None = None) -> dict[str, Any]:
    """Return a bounded, UI-safe progress projection."""

    target = budget_owner or value
    budget = budget_from_dialogue(target)
    total_tokens = int(getattr(target, "total_tokens", 0) or 0)
    cost_micros = getattr(target, "cost_micros", None)
    return {
        "max_turns": budget.max_turns,
        "turn_count": int(getattr(target, "turn_count", 0) or 0),
        "max_depth": budget.max_depth,
        "max_attempts": budget.max_attempts,
        "attempt_count": int(getattr(target, "attempt_count", 0) or 0),
        "max_tokens": budget.max_tokens,
        "total_tokens": total_tokens,
        "reserved_tokens": int(getattr(target, "reserved_tokens", 0) or 0),
        "remaining_tokens": max(0, budget.max_tokens - total_tokens - int(getattr(target, "reserved_tokens", 0) or 0)) if budget.max_tokens is not None else None,
        "max_cost_micros": budget.max_cost_micros,
        "cost_micros": int(cost_micros) if cost_micros is not None else None,
        "reserved_cost_micros": int(getattr(target, "reserved_cost_micros", 0) or 0),
        "cost_currency": budget.cost_currency or getattr(target, "cost_currency", None),
        "usage_state": _text(getattr(target, "usage_state", "known"), 24) or "known",
    }


__all__ = [
    "DEFAULT_DIALOGUE_BUDGET",
    "LOCAL_USAGE_SCOPE",
    "MAX_DIALOGUE_BUDGET",
    "DialogueBudget",
    "DialogueBudgetError",
    "DialogueUsageSnapshot",
    "assert_dialogue_budget_available",
    "budget_from_dialogue",
    "normalize_dialogue_budgets",
    "normalize_dialogue_usage",
    "project_dialogue_budget",
    "get_dialogue_budget_owner",
    "charge_dialogue_budget",
    "record_dialogue_usage",
    "release_dialogue_budget",
    "reserve_dialogue_budget",
]
