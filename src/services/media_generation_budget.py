"""Money admission for MediaOps children of a pinned AgentWork root.

Generic WorkItem quota remains in budget_reservation; these reservations are
server-owned metadata and count once per generation slot. No price is inferred
from quota units, provider names, or an unmetered flag.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import String, cast, inspect, or_, select, text, update

from ..memory.models import AgentRevision, AgentWorkItem
from ..memory.models.agent_work import _safe_json
from .media_operations_service import MediaOperationsValidationError, sha256_json

COST_LEDGER_KEY = "media_generation_costs"
MAX_HOLDS = 64


def _deny(reason: str) -> None:
    raise MediaOperationsValidationError(f"generation_budget_{reason}")


def _micros(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        _deny("amount_invalid")
    return value


def _currency(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or not value.isascii()
        or not value.isupper()
        or not value.isalpha()
    ):
        _deny("currency_invalid")
    return value


def _holds(row: AgentWorkItem) -> list[dict[str, Any]]:
    values = (row.metadata_json or {}).get(COST_LEDGER_KEY, [])
    if not isinstance(values, list) or len(values) > MAX_HOLDS:
        _deny("ledger_invalid")
    result = []
    slots = set()
    for item in values:
        if not isinstance(item, dict) or item.get("state") not in {"reserved", "spent"}:
            _deny("ledger_invalid")
        _micros(item.get("amount_micros"))
        _currency(item.get("currency"))
        if (
            not isinstance(item.get("day"), str)
            or not isinstance(item.get("slot"), str)
            or item["slot"] in slots
        ):
            _deny("ledger_invalid")
        try:
            date.fromisoformat(item["day"])
            for key in ("work_id", "agent_id", "agent_revision_id"):
                UUID(item[key])
        except (ValueError, TypeError, KeyError, AttributeError):
            _deny("ledger_invalid")
        for key in ("request_hash", "external_key_hash"):
            value = item.get(key)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                _deny("ledger_invalid")
        slots.add(item["slot"])
        result.append(dict(item))
    return result


async def _lock_agent(session: Any, agent_id: Any, root_id: Any) -> None:
    if session.get_bind().dialect.name == "postgresql":
        # Same Agent across all roots: a root lock alone cannot fence a daily cap.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"media-generation-cost:{agent_id}"},
        )
    await session.execute(
        update(AgentWorkItem)
        .where(AgentWorkItem.id == root_id)
        .values(updated_at=AgentWorkItem.updated_at)
    )


async def _root(session: Any, work: AgentWorkItem, project_id: Any) -> AgentWorkItem:
    current = work
    seen = set()
    declared_roots = set()
    while True:
        if current.id in seen or len(seen) >= 17 or current.project_id != project_id:
            _deny("ancestry_invalid")
        seen.add(current.id)
        if current.root_work_item_id is not None:
            declared_roots.add(current.root_work_item_id)
        revision = (
            await session.get(AgentRevision, current.agent_revision_id)
            if current.agent_revision_id
            else None
        )
        if revision is None or revision.agent_id != current.assigned_agent_id:
            _deny("agent_pin_invalid")
        if current.parent_work_item_id is None:
            break
        current = await session.get(AgentWorkItem, current.parent_work_item_id)
        if current is None:
            _deny("ancestry_invalid")
    if declared_roots - {current.id}:
        _deny("ancestry_invalid")
    return current


async def validate_generation_work(
    session: Any,
    work_item_id: Any,
    *,
    source_type: str,
    source_id: Any,
    source_revision: Any,
    project_id: Any,
) -> AgentWorkItem:
    try:
        work_id = UUID(str(work_item_id))
    except (ValueError, TypeError, AttributeError):
        _deny("work_invalid")
    work = await session.get(AgentWorkItem, work_id)
    if work is None or (
        work.source_type,
        work.source_id,
        work.source_revision,
        work.project_id,
    ) != (source_type, str(source_id), str(source_revision), project_id):
        _deny("source_pin_invalid")
    await _root(session, work, project_id)
    return work


async def reserve_generation_cost(
    session: Any,
    *,
    work_item_id: Any,
    source_type: str,
    source_id: Any,
    source_revision: Any,
    project_id: Any,
    slot: str,
    request_hash: str,
    external_key: str,
    cost_policy: Mapping[str, Any],
    existing_only: bool = False,
) -> Any:
    """Reserve in the caller's transaction, before its durable submit commit."""
    if work_item_id is None:
        return None  # Existing human/quota-only sessions retain their contract.
    work = await validate_generation_work(
        session,
        work_item_id,
        source_type=source_type,
        source_id=source_id,
        source_revision=source_revision,
        project_id=project_id,
    )
    root = await _root(session, work, project_id)
    revision = await session.get(AgentRevision, root.agent_revision_id)
    policy = revision.budget_policy_json or {}
    if not isinstance(policy, Mapping) or not isinstance(cost_policy, Mapping):
        _deny("policy_invalid")
    if any(
        hold.get("agent_id") != str(root.assigned_agent_id)
        or hold.get("agent_revision_id") != str(root.agent_revision_id)
        for hold in _holds(root)
    ):
        _deny("agent_pin_invalid")
    limits = {
        key: _micros(policy[key])
        for key in ("max_run_cost_micros", "max_daily_cost_micros")
        if key in policy
    }
    if not limits:
        return None
    currency = _currency(policy.get("currency"))
    # An explicit owner-pinned recipe/action estimate, or the existing Work
    # estimate. Never interpret generic reservation_amount/units as money.
    quote = (
        cost_policy
        if "estimated_cost_micros" in cost_policy
        else (work.budget_reservation_json or {})
    )
    amount = _micros(quote.get("estimated_cost_micros"))
    if _currency(quote.get("currency")) != currency:
        _deny("currency_mismatch")
    initial_pin = (
        root.assigned_agent_id,
        root.agent_revision_id,
        root.project_id,
        root.parent_work_item_id,
    )
    await _lock_agent(session, root.assigned_agent_id, root.id)
    agent_id = str(root.assigned_agent_id)
    roots = (
        await session.scalars(
            select(AgentWorkItem)
            .where(
                or_(
                    AgentWorkItem.assigned_agent_id == root.assigned_agent_id,
                    cast(AgentWorkItem.metadata_json, String).contains(agent_id),
                ),
                AgentWorkItem.parent_work_item_id.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    root = next((row for row in roots if row.id == root.id), None)
    if root is None:
        _deny("ancestry_invalid")
    if initial_pin != (
        root.assigned_agent_id,
        root.agent_revision_id,
        root.project_id,
        root.parent_work_item_id,
    ):
        _deny("agent_pin_changed")
    await session.refresh(work)
    await validate_generation_work(
        session,
        work.id,
        source_type=source_type,
        source_id=source_id,
        source_revision=source_revision,
        project_id=project_id,
    )
    ledger = _holds(root)
    identity = {
        "slot": slot,
        "request_hash": request_hash,
        "external_key_hash": sha256_json(external_key),
        "work_id": str(work.id),
        "agent_id": agent_id,
        "agent_revision_id": str(root.agent_revision_id),
        "currency": currency,
        "amount_micros": amount,
    }
    day = datetime.now(timezone.utc).date().isoformat()
    for row in roots:
        for hold in _holds(row):
            if hold["slot"] == slot:
                if row.id != root.id or any(
                    hold.get(key) != value for key, value in identity.items()
                ):
                    _deny("slot_binding_changed")
                return root.id
    if existing_only:
        _deny("reservation_missing")
    run_total = sum(item["amount_micros"] for item in ledger)
    # Currency changes cannot erase already reserved/spent amounts for a root.
    if any(item["currency"] != currency for item in ledger):
        _deny("currency_mismatch")
    daily_holds = [
        item
        for row in roots
        for item in _holds(row)
        if item["day"] == day and item.get("agent_id") == agent_id
    ]
    if any(
        item["currency"] != currency and item["amount_micros"] > 0
        for item in daily_holds
    ):
        _deny("currency_mismatch")
    daily_total = sum(item["amount_micros"] for item in daily_holds)
    if (
        "max_run_cost_micros" in limits
        and run_total + amount > limits["max_run_cost_micros"]
    ) or (
        "max_daily_cost_micros" in limits
        and daily_total + amount > limits["max_daily_cost_micros"]
    ):
        _deny("limit_exceeded")
    if len(ledger) >= MAX_HOLDS:
        _deny("ledger_full")
    metadata = dict(root.metadata_json or {})
    metadata[COST_LEDGER_KEY] = [*ledger, {**identity, "day": day, "state": "reserved"}]
    if _safe_json(metadata) != metadata:
        _deny("ledger_projection_invalid")
    # Refreshing the locked row above avoids stale ORM metadata overwrites.
    root.metadata_json = metadata
    await session.flush()
    return root.id


async def observe_generation_cost(
    session: Any,
    *,
    slot: str,
    request_hash: str,
    external_key: str,
    external_run_id: Any,
    status: str,
    root_id: Any = None,
) -> None:
    """Bind a validated receipt. Unknown and terminal failures keep the hold.

    73 has no typed money/refund proof. A failed/cancelled/unavailable receipt
    cannot establish that execution cost zero, so it never releases money.
    """
    if root_id is None:
        connection = await session.connection()
        if not await connection.run_sync(
            lambda conn: inspect(conn).has_table(AgentWorkItem.__tablename__)
        ):
            return
        candidates = (
            await session.scalars(
                select(AgentWorkItem).where(
                    cast(AgentWorkItem.metadata_json, String).contains(slot),
                    AgentWorkItem.parent_work_item_id.is_(None),
                )
            )
        ).all()
        matches = [
            row
            for row in candidates
            if any(hold["slot"] == slot for hold in _holds(row))
        ]
        if not matches:
            return
        if len(matches) != 1:
            _deny("slot_binding_changed")
        root_id = matches[0].id
    root = await session.get(AgentWorkItem, root_id)
    if root is None:
        _deny("ancestry_invalid")
    await _lock_agent(session, root.assigned_agent_id, root.id)
    await session.refresh(root)
    ledger = _holds(root)
    found = False
    for hold in ledger:
        if hold["slot"] != slot:
            continue
        found = True
        if (
            hold.get("request_hash") != request_hash
            or hold.get("external_key_hash") != sha256_json(external_key)
            or (
                hold.get("external_run_id")
                and hold["external_run_id"] != external_run_id
            )
        ):
            _deny("receipt_binding_changed")
        if external_run_id:
            hold["external_run_id"] = external_run_id
        if status in {"succeeded", "complete", "completed", "success"}:
            hold["state"] = "spent"
    if not found:
        _deny("slot_binding_changed")
    metadata = {**(root.metadata_json or {}), COST_LEDGER_KEY: ledger}
    if _safe_json(metadata) != metadata:
        _deny("ledger_projection_invalid")
    root.metadata_json = metadata
