"""Pure, fail-closed projections of event-scoped revenue cost evidence."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any


COST_AMOUNT_FIELDS = ("fee_amount", "production_cost_amount", "human_cost_amount")
COST_INPUT_FIELDS = (*COST_AMOUNT_FIELDS, "human_minutes")


def _get(row: Any, name: str, default: Any = None) -> Any:
    return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)


def _amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and abs(value) <= 1_000_000_000_000 else None


def effective_cost_records(records: list[Any]) -> dict[str, Any]:
    """Choose one immutable revision per event; never add correction versions."""
    result: dict[str, Any] = {}
    for record in records:
        key = str(_get(record, "revenue_event_id"))
        previous = result.get(key)
        if previous is None or _get(record, "revision", 0) > _get(previous, "revision", 0):
            result[key] = record
    return result


def revenue_cost_projection(event: Any, record: Any = None) -> dict[str, Any]:
    """Profit covers three explicit cost categories allocated to one event.

    A fee is independently evidenced and reconciled to gross minus net; it is
    already deducted from net. Human time is never implicitly priced at zero.
    This is an input-scope calculation, not business-wide or tax net income.
    """
    result = {
        **{name: None for name in COST_INPUT_FIELDS},
        "cost_record_id": None, "cost_revision": None,
        "cost_authority": "unknown", "cost_coverage_complete": False,
        "cost_missing_reasons": {}, "cost_coverage_note": None,
        "profit_amount": None, "profit_status": "unavailable_cost_record",
        "profit_basis": "net_less_production_and_human_cost",
        "cost_attribution_method": "direct_event",
    }
    if record is None:
        return result
    result.update(
        cost_record_id=str(_get(record, "id")),
        cost_revision=_get(record, "revision"),
    )
    # Do not permit a corrupt or wrongly joined row to change attribution.
    if not re.fullmatch(r"[A-Z]{3}", str(_get(event, "currency", ""))) or not re.fullmatch(r"[0-9a-f]{64}", str(_get(event, "event_hash", ""))):
        result["profit_status"] = "unavailable_cost_scope"
        return result
    if any(str(_get(event, target)) != str(_get(record, source)) for target, source in (
        ("id", "revenue_event_id"), ("event_hash", "revenue_event_hash"),
        ("owner_user_id", "owner_user_id"), ("project_id", "project_id"),
        ("currency", "currency"), ("event_at", "window_start"), ("event_at", "window_end"),
    )) or _get(record, "attribution_method") != "direct_event":
        result["profit_status"] = "unavailable_cost_scope"
        return result
    source, basis = _get(record, "source"), _get(record, "basis")
    if source not in {"manual", "imported"} or basis not in {"estimated", "settled"}:
        result["profit_status"] = "unavailable_cost_authority"
        return result
    result.update(
        **{name: _amount(_get(record, name)) for name in COST_INPUT_FIELDS},
        cost_authority=f"{source}_{'estimate' if basis == 'estimated' else 'settled'}",
        cost_coverage_complete=_get(record, "coverage_complete") is True,
        cost_missing_reasons=_get(record, "missing_reasons", {}) or {},
        cost_coverage_note=_get(record, "coverage_note"),
    )
    revenue_basis = "settled" if _get(event, "settlement_at") is not None else "estimated"
    if _get(event, "source") not in {"manual", "imported", "api"} or _get(event, "event_type") not in {"sale", "refund", "chargeback", "adjustment", "reversal"}:
        result["profit_status"] = "unavailable_cost_authority"
    elif not _get(record, "evidence") or not str(_get(record, "coverage_note") or "").strip():
        result["profit_status"] = "unavailable_cost_coverage"
    elif _get(event, "event_type") == "sale" and any(result[name] is not None and result[name] < 0 for name in COST_AMOUNT_FIELDS):
        result["profit_status"] = "unavailable_invalid_amount"
    elif basis != revenue_basis:
        result["profit_status"] = "unavailable_basis_mismatch"
    elif not result["cost_coverage_complete"]:
        result["profit_status"] = "unavailable_cost_coverage"
    elif any(result[name] is None for name in COST_AMOUNT_FIELDS):
        result["profit_status"] = "unavailable_cost_inputs"
    elif _amount(_get(event, "gross_amount")) is None or _amount(_get(event, "net_amount")) is None:
        result["profit_status"] = "unavailable_invalid_amount"
    elif (Decimal(str(_get(event, "gross_amount"))) - Decimal(str(_get(event, "net_amount")))) != Decimal(str(result["fee_amount"])):
        result["profit_status"] = "unavailable_fee_reconciliation"
    else:
        profit = float(Decimal(str(_get(event, "net_amount"))) - Decimal(str(result["production_cost_amount"])) - Decimal(str(result["human_cost_amount"])))
        result["profit_amount"] = _amount(profit)
        result["profit_status"] = "calculated_input_scope" if result["profit_amount"] is not None else "unavailable_invalid_amount"
    return result
