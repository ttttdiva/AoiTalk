"""Pure, bounded owner-confirmed finance snapshot aggregation (F2 slice).

This module is a *pure computation* slice for the ``private.finance.summary``
contract. It performs no database, ORM, API, crypto or persistence work and makes
no decryption or persistence claim.

Trusted-caller boundary
-----------------------
:class:`FinanceSummarySnapshot` and :class:`FinanceSummaryRow` are internal DTOs.
A future owner-checked repository adapter must construct them after verifying
dataset ownership and decrypting the payload; that adapter, the accepted-dataset
persistence and the DB snapshot loader are explicitly pending and are not faked
here. Model input is never coerced into an ``owner_confirmed`` state by this
module, and the future loader must owner-check the dataset before decryption.

Contract output (exactly four keys)
-----------------------------------
::

    {
        "snapshot_digest": "<64 lowercase hex>",
        "totals": [
            {
                "currency": "JPY",
                "income_minor": 0,
                "expense_minor": 0,
                "record_count": 0,
            },
        ],
        "duplicate_candidates": ["<uuid str>"],
        "needs_review": ["snapshot_incomplete"],
    }

Rules enforced here
-------------------
* owner, dataset and ``immutable`` are validated before any row is read;
* only ``owner_confirmed`` income/expense rows of the requested month total;
* balance snapshots never total, and ``unconfirmed`` / ``possible_duplicate``
  rows never total (possible duplicates are itemised as row refs);
* an identical replay of the same ``row_ref`` counts once, while a conflicting
  replay of the same ``row_ref`` fails as an integrity error;
* ``totals`` is ``[]`` when nothing qualifies, which is distinct from an explicit
  ``0`` for a currency that has contributing rows;
* all arithmetic is exact Python ``int`` and every aggregate is capped at
  ``MAX_SAFE_INTEGER`` with an explicit overflow error instead of rounding;
* no exchange rate, netting, balance inference, monetary parser or formatter.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, Literal
from uuid import UUID

__all__ = [
    "MAX_ROWS",
    "MAX_SAFE_INTEGER",
    "FinanceSummaryIntegrityError",
    "FinanceSummaryOverflowError",
    "FinanceSummaryRow",
    "FinanceSummarySnapshot",
    "FinanceSummaryTrustError",
    "FinanceSummaryValidationError",
    "summarize_finance_snapshot",
]

#: Largest exactly representable integer, matching the client contract bound.
MAX_SAFE_INTEGER: Final[int] = 9_007_199_254_740_991

#: Hard bound on rows consumed from a single snapshot.
MAX_ROWS: Final[int] = 1_000

#: Canonical digest domain separator; bump only with a contract change.
_DIGEST_VERSION: Final[str] = "f2.personal_finance_summary.v1"

KIND_INCOME: Final[str] = "income"
KIND_EXPENSE: Final[str] = "expense"
KIND_BALANCE: Final[str] = "balance"

STATE_OWNER_CONFIRMED: Final[str] = "owner_confirmed"
STATE_UNCONFIRMED: Final[str] = "unconfirmed"
STATE_POSSIBLE_DUPLICATE: Final[str] = "possible_duplicate"

_NEEDS_REVIEW_INCOMPLETE: Final[str] = "snapshot_incomplete"

_KINDS: Final[frozenset[str]] = frozenset({KIND_INCOME, KIND_EXPENSE, KIND_BALANCE})
_TOTALED_KINDS: Final[frozenset[str]] = frozenset({KIND_INCOME, KIND_EXPENSE})
_CONFIRMATION_STATES: Final[frozenset[str]] = frozenset(
    {STATE_OWNER_CONFIRMED, STATE_UNCONFIRMED, STATE_POSSIBLE_DUPLICATE}
)
_CURRENCY_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Z]{3}")
_MONTH_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")

SummaryKind = Literal["income", "expense", "balance"]
ConfirmationState = Literal["owner_confirmed", "unconfirmed", "possible_duplicate"]
RowContent = tuple[str, str, int, str, str]


class FinanceSummaryValidationError(ValueError):
    """A DTO violates the bounded snapshot shape or a field's explicit domain."""


class FinanceSummaryTrustError(PermissionError):
    """The snapshot is not bound to the accepted owner, dataset or immutable flag."""


class FinanceSummaryIntegrityError(ValueError):
    """A conflicting replay reuses an existing ``row_ref`` with different content."""


class FinanceSummaryOverflowError(ArithmeticError):
    """An aggregate would exceed ``MAX_SAFE_INTEGER``; rounding is never applied."""


@dataclass(frozen=True, slots=True)
class FinanceSummaryRow:
    """One reviewed row of an already owner-checked, decrypted snapshot."""

    row_ref: UUID
    occurred_on: str
    kind: SummaryKind
    amount_minor: int
    currency: str
    confirmation_state: ConfirmationState


@dataclass(frozen=True, slots=True)
class FinanceSummarySnapshot:
    """Trusted-caller snapshot input.

    ``owner_user_id`` / ``dataset_ref`` / ``immutable`` / ``complete`` are
    metadata supplied by the future repository adapter; ``rows`` are consumed only
    after those metadata pass validation. This pure slice never decrypts, loads
    or persists anything.
    """

    owner_user_id: UUID
    dataset_ref: UUID
    immutable: bool
    complete: bool
    rows: tuple[FinanceSummaryRow, ...]


def summarize_finance_snapshot(
    snapshot: FinanceSummarySnapshot,
    *,
    owner_user_id: UUID,
    accepted_dataset_ref: UUID,
    month: str,
) -> dict[str, Any]:
    """Aggregate one bounded owner-confirmed snapshot for ``month`` (``YYYY-MM``).

    Raises:
        FinanceSummaryTrustError: owner/dataset mismatch or non-immutable snapshot.
        FinanceSummaryValidationError: malformed month, flags, rows or row fields.
        FinanceSummaryIntegrityError: conflicting replay of one ``row_ref``.
        FinanceSummaryOverflowError: an aggregate exceeds ``MAX_SAFE_INTEGER``.
    """

    # Trust metadata is checked before any row is read.
    _validate_scope(
        snapshot,
        owner_user_id=owner_user_id,
        accepted_dataset_ref=accepted_dataset_ref,
    )
    checked_month = _validate_month(month)
    rows = _validate_rows(snapshot.rows)

    unique_rows = _dedupe_rows(rows)
    totals, duplicate_candidates = _aggregate(unique_rows, month=checked_month)
    needs_review: list[str] = [] if snapshot.complete else [_NEEDS_REVIEW_INCOMPLETE]
    snapshot_digest = _snapshot_digest(
        owner_user_id=owner_user_id,
        dataset_ref=accepted_dataset_ref,
        month=checked_month,
        complete=snapshot.complete,
        rows=unique_rows,
    )

    return {
        "snapshot_digest": snapshot_digest,
        "totals": totals,
        "duplicate_candidates": duplicate_candidates,
        "needs_review": needs_review,
    }


def _validate_scope(
    snapshot: FinanceSummarySnapshot,
    *,
    owner_user_id: UUID,
    accepted_dataset_ref: UUID,
) -> None:
    if not isinstance(snapshot, FinanceSummarySnapshot):
        raise FinanceSummaryValidationError(
            "snapshot must be a FinanceSummarySnapshot built by the repository adapter"
        )
    if not isinstance(owner_user_id, UUID):
        raise FinanceSummaryValidationError("owner_user_id must be a UUID")
    if not isinstance(accepted_dataset_ref, UUID):
        raise FinanceSummaryValidationError("accepted_dataset_ref must be a UUID")

    if snapshot.owner_user_id != owner_user_id:
        raise FinanceSummaryTrustError(
            "snapshot owner_user_id does not match the accepted owner"
        )
    if snapshot.dataset_ref != accepted_dataset_ref:
        raise FinanceSummaryTrustError(
            "snapshot dataset_ref does not match the accepted dataset"
        )
    if snapshot.immutable is not True:
        raise FinanceSummaryTrustError(
            "snapshot must be immutable before it can be aggregated"
        )
    if not isinstance(snapshot.complete, bool):
        raise FinanceSummaryValidationError("snapshot complete must be an explicit bool")


def _validate_month(month: str) -> str:
    if not isinstance(month, str) or _MONTH_PATTERN.fullmatch(month) is None:
        raise FinanceSummaryValidationError(
            f"month must be an explicit YYYY-MM string, got {month!r}"
        )
    if month.startswith("0000-"):
        raise FinanceSummaryValidationError(
            f"month year must be 0001-9999, got {month!r}"
        )
    return month


def _validate_rows(rows: tuple[FinanceSummaryRow, ...]) -> tuple[FinanceSummaryRow, ...]:
    if not isinstance(rows, tuple):
        raise FinanceSummaryValidationError("snapshot rows must be a tuple")
    if len(rows) > MAX_ROWS:
        raise FinanceSummaryValidationError(
            f"snapshot rows exceed the {MAX_ROWS}-row bound ({len(rows)} given)"
        )
    for index, row in enumerate(rows):
        _validate_row(row, index=index)
    return rows


def _validate_row(row: FinanceSummaryRow, *, index: int) -> None:
    if not isinstance(row, FinanceSummaryRow):
        raise FinanceSummaryValidationError(f"rows[{index}] must be a FinanceSummaryRow")
    if not isinstance(row.row_ref, UUID):
        raise FinanceSummaryValidationError(f"rows[{index}].row_ref must be a UUID")
    if not _is_explicit_iso_date(row.occurred_on):
        raise FinanceSummaryValidationError(
            f"rows[{index}].occurred_on must be an explicit ISO date (YYYY-MM-DD)"
        )
    if not isinstance(row.kind, str) or row.kind not in _KINDS:
        raise FinanceSummaryValidationError(
            f"rows[{index}].kind must be one of {sorted(_KINDS)}"
        )
    amount_minor = row.amount_minor
    if type(amount_minor) is not int:
        raise FinanceSummaryValidationError(
            f"rows[{index}].amount_minor must be an exact int (bool/float rejected)"
        )
    if amount_minor < 0 or amount_minor > MAX_SAFE_INTEGER:
        raise FinanceSummaryValidationError(
            f"rows[{index}].amount_minor must be within 0..{MAX_SAFE_INTEGER}"
        )
    if not isinstance(row.currency, str) or _CURRENCY_PATTERN.fullmatch(row.currency) is None:
        raise FinanceSummaryValidationError(
            f"rows[{index}].currency must be an explicit uppercase 3-letter code"
        )
    if (
        not isinstance(row.confirmation_state, str)
        or row.confirmation_state not in _CONFIRMATION_STATES
    ):
        raise FinanceSummaryValidationError(
            f"rows[{index}].confirmation_state must be one of {sorted(_CONFIRMATION_STATES)}"
        )


def _is_explicit_iso_date(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    if value[4] != "-" or value[7] != "-":
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def _dedupe_rows(rows: tuple[FinanceSummaryRow, ...]) -> list[tuple[UUID, RowContent]]:
    """Collapse identical replays and reject conflicting reuse of one ``row_ref``."""

    seen: dict[UUID, RowContent] = {}
    for row in rows:
        content: RowContent = (
            row.occurred_on,
            row.kind,
            row.amount_minor,
            row.currency,
            row.confirmation_state,
        )
        previous = seen.get(row.row_ref)
        if previous is None:
            seen[row.row_ref] = content
        elif previous != content:
            raise FinanceSummaryIntegrityError(
                f"conflicting replay for row_ref {row.row_ref}"
            )
    return sorted(seen.items(), key=lambda item: str(item[0]))


def _aggregate(
    unique_rows: list[tuple[UUID, RowContent]],
    *,
    month: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    buckets: dict[str, dict[str, int]] = {}
    duplicate_candidates: list[str] = []

    for row_ref, content in unique_rows:
        occurred_on, kind, amount_minor, currency, confirmation_state = content
        if kind not in _TOTALED_KINDS:
            # Balance snapshots are never inferred into income/expense totals.
            continue
        if occurred_on[:7] != month:
            continue
        if confirmation_state == STATE_POSSIBLE_DUPLICATE:
            duplicate_candidates.append(str(row_ref))
            continue
        if confirmation_state != STATE_OWNER_CONFIRMED:
            continue

        bucket = buckets.get(currency)
        if bucket is None:
            bucket = {"income_minor": 0, "expense_minor": 0, "record_count": 0}
            buckets[currency] = bucket
        if kind == KIND_INCOME:
            bucket["income_minor"] = _add_capped(
                bucket["income_minor"], amount_minor, aggregate=f"{currency}.income_minor"
            )
        else:
            bucket["expense_minor"] = _add_capped(
                bucket["expense_minor"], amount_minor, aggregate=f"{currency}.expense_minor"
            )
        bucket["record_count"] = _add_capped(
            bucket["record_count"], 1, aggregate=f"{currency}.record_count"
        )

    totals: list[dict[str, Any]] = [
        {
            "currency": currency,
            "income_minor": buckets[currency]["income_minor"],
            "expense_minor": buckets[currency]["expense_minor"],
            "record_count": buckets[currency]["record_count"],
        }
        for currency in sorted(buckets)
    ]
    return totals, sorted(duplicate_candidates)


def _add_capped(current: int, amount: int, *, aggregate: str) -> int:
    total = current + amount
    if total > MAX_SAFE_INTEGER:
        raise FinanceSummaryOverflowError(
            f"aggregate {aggregate} exceeds MAX_SAFE_INTEGER ({MAX_SAFE_INTEGER})"
        )
    return total


def _snapshot_digest(
    *,
    owner_user_id: UUID,
    dataset_ref: UUID,
    month: str,
    complete: bool,
    rows: list[tuple[UUID, RowContent]],
) -> str:
    payload = {
        "version": _DIGEST_VERSION,
        "owner_user_id": str(owner_user_id),
        "dataset_ref": str(dataset_ref),
        "month": month,
        "complete": complete,
        "rows": [
            {
                "row_ref": str(row_ref),
                "occurred_on": occurred_on,
                "kind": kind,
                "amount_minor": amount_minor,
                "currency": currency,
                "confirmation_state": confirmation_state,
            }
            for row_ref, (
                occurred_on,
                kind,
                amount_minor,
                currency,
                confirmation_state,
            ) in rows
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
