"""Privacy-safe decision receipts for bounded semantic routing.

Receipts intentionally retain only opaque candidate IDs, numeric
signals, Host outcomes, and digests of raw state/candidate payloads. Raw prompts,
memory text, DOM, credentials, and candidate values are never stored here.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
import re
from threading import RLock
from typing import Any, Iterable, Mapping
from uuid import uuid4

logger = logging.getLogger(__name__)

DECISION_CONTRACT_VERSION = "1"
_RECENT_RECEIPTS: OrderedDict[str, "DecisionReceipt"] = OrderedDict()
_RECEIPT_LOCK = RLock()


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda item: f"<{type(item).__name__}>",
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return repr(type(value).__name__).encode("utf-8")


def decision_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _code(value: Any) -> str:
    """Only Host-owned status codes, never exception messages or action text."""
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", value):
        return ""
    if any(secret and secret in value for name, secret in os.environ.items()
           if re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", name, re.I)):
        return ""
    if re.search(r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,})", value):
        return ""
    return value


def _candidate_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return _code(value) or "id_" + decision_digest(value)


def _safe_probability_map(values: Mapping[str, Any] | None, *, allowed: set[str] | None = None) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, value in (values or {}).items():
        if len(result) >= 255:
            break
        if not isinstance(key, str) or not key or (allowed is not None and key not in allowed):
            continue
        if (
            type(value) in {int, float}
            and math.isfinite(value)
            and 0.0 <= float(value) <= 1.0
        ):
            result[_candidate_id(key)] = float(value)
    return result


@dataclass(frozen=True)
class DecisionReceipt:
    decision_id: str
    created_at: str
    decision_kind: str
    contract_version: str
    state_digest: str
    candidate_set_digest: str
    candidate_ids: tuple[str, ...]
    selected_candidate: str = ""
    probabilities: Mapping[str, float] = field(default_factory=dict)
    probability_candidate_ids: tuple[str, ...] = ()
    probability_scope: str = "unavailable"
    signals: Mapping[str, float] = field(default_factory=dict)
    confidence: float | None = None
    threshold: float | None = None
    engine: str = ""
    fallback_reason: str = ""
    latency_ms: float = 0.0
    validation_result: str = "decision_only"
    host_action: str = ""
    host_candidate: str = ""
    outcome: str = "pending"
    baseline_candidate: str = ""
    shadow_candidate: str = ""
    agreement: bool | None = None
    human_override: bool = False
    shadow_mode: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["candidate_ids"] = list(self.candidate_ids)
        value["probability_candidate_ids"] = list(self.probability_candidate_ids)
        return value


def create_decision_receipt(
    *,
    decision_kind: str,
    state: Any,
    candidate_ids: Iterable[str],
    candidate_snapshot: Any = None,
    selected_candidate: str = "",
    probabilities: Mapping[str, Any] | None = None,
    signals: Mapping[str, Any] | None = None,
    confidence: float | None = None,
    threshold: float | None = None,
    engine: str = "",
    fallback_reason: str = "",
    latency_ms: float = 0.0,
    validation_result: str = "decision_only",
    shadow_mode: bool = False,
) -> DecisionReceipt:
    raw_ids = tuple(dict.fromkeys(item for item in candidate_ids if isinstance(item, str) and item))[:4096]
    ids = tuple(_candidate_id(item) for item in raw_ids)
    safe_confidence = (
        float(confidence)
        if type(confidence) in {int, float}
        and math.isfinite(confidence)
        and 0.0 <= float(confidence) <= 1.0
        else None
    )
    safe_threshold = (
        float(threshold)
        if type(threshold) in {int, float}
        and math.isfinite(threshold)
        and 0.0 <= float(threshold) <= 1.0
        else None
    )
    safe_latency = (
        max(0.0, float(latency_ms))
        if type(latency_ms) in {int, float} and math.isfinite(latency_ms)
        else 0.0
    )
    safe_probabilities = _safe_probability_map(probabilities, allowed=set(raw_ids) | {"none"})
    return DecisionReceipt(
        decision_id=uuid4().hex,
        created_at=datetime.now(timezone.utc).isoformat(),
        decision_kind=_code(decision_kind),
        contract_version=DECISION_CONTRACT_VERSION,
        state_digest=decision_digest(state),
        candidate_set_digest=decision_digest(
            candidate_snapshot if candidate_snapshot is not None else ids
        ),
        candidate_ids=ids,
        selected_candidate=_candidate_id(selected_candidate) if selected_candidate in (*raw_ids, "none") else "",
        probabilities=safe_probabilities,
        probability_candidate_ids=tuple(safe_probabilities),
        probability_scope=("unavailable" if not safe_probabilities else
                           "candidate_set" if set(safe_probabilities) - {"none"} == set(ids)
                           else "evaluated_subset"),
        signals=_safe_probability_map(signals),
        confidence=safe_confidence,
        threshold=safe_threshold,
        engine=_code(engine),
        fallback_reason=_code(fallback_reason),
        latency_ms=safe_latency,
        validation_result=_code(validation_result),
        shadow_mode=bool(shadow_mode),
    )


def record_decision_receipt(receipt: DecisionReceipt) -> DecisionReceipt:
    with _RECEIPT_LOCK:
        _RECENT_RECEIPTS[receipt.decision_id] = receipt
        _RECENT_RECEIPTS.move_to_end(receipt.decision_id)
        while len(_RECENT_RECEIPTS) > 2048:
            _RECENT_RECEIPTS.popitem(last=False)
    logger.info(
        "decision_receipt=%s",
        json.dumps(receipt.to_dict(), ensure_ascii=False, separators=(",", ":")),
    )
    return receipt


def finalize_decision_receipt(
    receipt: DecisionReceipt | None,
    *,
    host_action: str | None = None,
    host_candidate: str | None = None,
    outcome: str,
    validation_result: str | None = None,
    baseline_candidate: str | None = None,
    shadow_candidate: str | None = None,
    agreement: bool | None = None,
    human_override: bool | None = None,
    shadow_mode: bool | None = None,
    record: bool = True,
) -> DecisionReceipt | None:
    if receipt is None:
        return None
    updated = replace(
        receipt,
        host_action=_code(host_action) if host_action is not None else receipt.host_action,
        host_candidate=_candidate_id(host_candidate) if host_candidate is not None else receipt.host_candidate,
        outcome=_code(outcome),
        validation_result=(
            _code(validation_result)
            if validation_result is not None
            else receipt.validation_result
        ),
        baseline_candidate=_candidate_id(baseline_candidate) if baseline_candidate is not None else receipt.baseline_candidate,
        shadow_candidate=_candidate_id(shadow_candidate) if shadow_candidate is not None else receipt.shadow_candidate,
        agreement=agreement if isinstance(agreement, bool) else receipt.agreement,
        human_override=bool(human_override) if human_override is not None else receipt.human_override,
        shadow_mode=bool(shadow_mode) if shadow_mode is not None else receipt.shadow_mode,
    )
    return record_decision_receipt(updated) if record else updated


def recent_decision_receipts(limit: int = 100) -> list[dict[str, Any]]:
    with _RECEIPT_LOCK:
        bounded = max(0, min(int(limit), len(_RECENT_RECEIPTS)))
        return [item.to_dict() for item in list(_RECENT_RECEIPTS.values())[-bounded:]] if bounded else []


def summarize_decision_receipts(
    receipts: Iterable[DecisionReceipt] | None = None,
) -> dict[str, Any]:
    with _RECEIPT_LOCK:
        rows = list({item.decision_id: item for item in (receipts if receipts is not None else _RECENT_RECEIPTS.values())}.values())
    by_kind = Counter(item.decision_kind for item in rows)
    outcomes = Counter(item.outcome for item in rows)
    agreements = [item.agreement for item in rows if item.agreement is not None]
    return {
        "count": len(rows),
        "by_kind": dict(by_kind),
        "outcomes": dict(outcomes),
        "agreement_count": sum(1 for value in agreements if value),
        "disagreement_count": sum(1 for value in agreements if not value),
        "agreement_rate": (
            sum(1 for value in agreements if value) / len(agreements)
            if agreements
            else None
        ),
    }
