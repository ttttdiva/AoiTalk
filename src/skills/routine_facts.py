"""Fail-closed matching of public, explicitly labelled business facts.

Browser text is evidence, not an instruction. Amounts and dates must parse in
full; duplicated labels, approximate dates and ambiguous currency never match.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

DEFAULT_LABELS = {"target": "対象", "datetime": "日時", "price": "税込総額"}
TOTAL_LABELS = ("税込総額", "税込合計", "支払総額(税込)", "grand total", "total including tax", "amount due")


def normalized(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def fact_value(key: str, value: str) -> Any:
    value = normalized(value)
    if key == "price":
        # A bare $ or ¥ does not establish USD or JPY. Full match also rejects
        # tax-exclusive/additional charges, ranges and alternative amounts.
        match = re.fullmatch(
            r"(?:(JPY|USD|US\$|EUR|€|GBP|£)\s*)?"
            r"([0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(\.[0-9]{1,2})?"
            r"\s*(円|JPY|USD|US\$|EUR|€|GBP|£)?(?:\s*\(税込\))?", value, re.I)
        if not match or not (match[1] or match[4]):
            raise ValueError("routine_money_ambiguous")
        currencies = {"円": "JPY", "JPY": "JPY", "USD": "USD", "US$": "USD",
                      "EUR": "EUR", "€": "EUR", "GBP": "GBP", "£": "GBP"}
        units = {currencies[v.upper()] for v in (match[1], match[4]) if v}
        if len(units) != 1:
            raise ValueError("routine_currency_ambiguous")
        currency = units.pop()
        amount = Decimal(match[2].replace(",", "") + (match[3] or ""))
        if currency == "JPY" and amount != amount.to_integral_value():
            raise ValueError("routine_yen_fraction_invalid")
        # Decimal.normalize() rounds through the ambient precision context.
        # Preserve every significant digit instead of collapsing large totals.
        canonical_amount = format(amount, "f")
        if "." in canonical_amount:
            canonical_amount = canonical_amount.rstrip("0").rstrip(".")
        return {"currency": currency, "amount": canonical_amount}
    if key == "datetime":
        match = re.fullmatch(
            r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?[ T]"
            r"(\d{1,2}):(\d{2})(?:\s*(Z|UTC|JST|[+-]\d{2}:\d{2}))?", value, re.I)
        if not match:
            raise ValueError("routine_datetime_ambiguous")
        stamp = datetime(int(match[1]), int(match[2]), int(match[3]), int(match[4]), int(match[5]))
        zone = (match[6] or "").upper()
        if zone:
            suffix = {"Z": "+00:00", "UTC": "+00:00", "JST": "+09:00"}.get(zone, zone)
            stamp = datetime.fromisoformat(stamp.isoformat() + suffix).astimezone(timezone.utc)
            return stamp.isoformat(timespec="minutes")
        return stamp.isoformat(sep=" ", timespec="minutes")
    if not value:
        raise ValueError("routine_fact_empty")
    return value


def labels_for(fields: dict[str, str], labels: dict[str, str]) -> dict[str, str]:
    result = {key: normalized(labels.get(key, DEFAULT_LABELS.get(key, key))) for key in fields}
    if (len(set(v.casefold() for v in result.values())) != len(result) or
            any(not v or re.search(r"[:=|;]", v) for v in result.values())):
        raise ValueError("routine_fact_label_ambiguous")
    if "price" in result and result["price"].casefold() not in {v.casefold() for v in TOTAL_LABELS}:
        raise ValueError("routine_tax_inclusive_total_label_required")
    return result


def verified_facts(text: str, fields: dict[str, str], labels: dict[str, str]) -> dict[str, Any] | None:
    """Read each entire labelled value once, including flattened DOM context.

    A value ends at a delimiter or the next known label. Labels need a boundary
    and a colon/equal sign, so '予約対象外:' cannot stand in for '対象:'.
    """
    try:
        expected_labels = labels_for(fields, labels)
        label_roles = {v.casefold(): k for k, v in expected_labels.items()}
        if "price" in fields:
            label_roles.update({v.casefold(): "price" for v in TOTAL_LABELS})
        text = normalized(text)
        pattern = re.compile(r"(?<![^\s|;/])(" + "|".join(
            re.escape(v) for v in sorted(label_roles, key=len, reverse=True)) + r")\s*[:=]\s*", re.I)
        matches = list(pattern.finditer(text))
        seen: dict[str, Any] = {}
        for index, match in enumerate(matches):
            key = label_roles[match[1].casefold()]
            if key in seen:
                return None
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            value = re.split(r"\s*[|;]\s*|\s+/\s+", text[match.end():end], maxsplit=1)[0].strip()
            seen[key] = fact_value(key, value)
        expected = {key: fact_value(key, value) for key, value in fields.items()}
        if seen != expected:
            return None
        display = {key: display_value(key, value) for key, value in seen.items()}
        return {"facts": seen, "fields": display}
    except (ValueError, TypeError, OverflowError):
        return None


def display_value(key: str, value: Any) -> str:
    if key == "price":
        return value["amount"] + ("円" if value["currency"] == "JPY" else " " + value["currency"])
    return str(value)


def completion_statement(text: str, expected: str, labels: dict[str, str]) -> bool:
    # Require a complete delimited statement. Quoted/negative/partial success
    # messages do not constitute a receipt.
    if normalized(expected) in [normalized(part) for part in re.split(r"[|;\n]", text)]:
        return True
    # safeText flattens heading/paragraph line breaks. A complete leading
    # statement may therefore end at the first explicit business-fact label.
    match = re.search(r"(?<![^\s|;/])(?:" + "|".join(re.escape(v) for v in labels.values()) +
                      r")\s*[:=]", normalized(text), re.I)
    return bool(match and normalized(text)[:match.start()].strip(" |;/") == normalized(expected))
