"""Strict, deterministic receipt text -> finance candidate fields.

Pure functions only: no float arithmetic, no network, no model.  Every value
carries the exact ``start``/``end`` character span of its source token in the
processor text.  Anything ambiguous is left out and listed in ``review_reasons``
so the owner decides; the parser never guesses.
"""

from __future__ import annotations

from datetime import date
import re

PARSER_VERSION = "receipt_text_v1"

# ISO 4217 minor-unit exponents for the currencies the receipt symbols map to.
CURRENCY_EXPONENT = {"JPY": 0, "USD": 2, "EUR": 2}
_SYMBOL_CURRENCY = {"¥": "JPY", "￥": "JPY", "円": "JPY", "$": "USD", "＄": "USD", "€": "EUR"}
MAX_AMOUNT_MINOR = 9_007_199_254_740_991

_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_AMOUNT_RE = re.compile(rf"(?P<pre>[¥￥$＄€])?\s?(?P<num>{_NUMBER})(?P<post>\s?円)?")
_TOTAL_LABEL_RE = re.compile(r"合\s*計|お買上げ?計|お支払い?金額|TOTAL|Total|total", re.IGNORECASE)
_EXCLUDED_LABEL_RE = re.compile(r"小\s*計|税|お預り|お釣|釣銭|sub\s*total|tax|change", re.IGNORECASE)
_DATE_RE = re.compile(r"(?<!\d)(?P<y>\d{4})\s?[-/.年]\s?(?P<m>\d{1,2})\s?[-/.月]\s?(?P<d>\d{1,2})\s?日?(?!\d)")


def parse_minor_units(token: str, currency: str) -> int:
    """Exact decimal text -> integer minor units, or ValueError."""

    exponent = CURRENCY_EXPONENT.get(currency)
    if exponent is None or not isinstance(token, str):
        raise ValueError("unsupported currency")
    if not re.fullmatch(_NUMBER, token):
        raise ValueError("malformed amount")
    whole, _, fraction = token.replace(",", "").partition(".")
    if fraction and len(fraction) != exponent:
        raise ValueError("fraction does not match currency exponent")
    if len(whole) > 1 and whole.startswith("0"):
        raise ValueError("leading zero")
    value = int(whole) * 10**exponent + (int(fraction) if fraction else 0)
    if value <= 0 or value > MAX_AMOUNT_MINOR:
        raise ValueError("amount out of range")
    return value


def _line_spans(text: str):
    offset = 0
    for line in text.split("\n"):
        yield offset, line
        offset += len(line) + 1


def _currency_of(match: re.Match[str]) -> str | None:
    symbol = match.group("pre") or (match.group("post") or "").strip() or None
    return _SYMBOL_CURRENCY.get(symbol) if symbol else None


def _extract_total(text: str, reasons: list[str]) -> dict | None:
    found: list[dict] = []
    for offset, line in _line_spans(text):
        label = _TOTAL_LABEL_RE.search(line)
        if label is None or _EXCLUDED_LABEL_RE.search(line):
            continue
        for match in _AMOUNT_RE.finditer(line, label.end()):
            currency = _currency_of(match)
            if currency is None:
                # A bare number has no currency evidence; JPY is not assumed.
                reasons.append("total_currency_unresolved")
                continue
            try:
                minor = parse_minor_units(match.group("num"), currency)
            except ValueError:
                reasons.append("total_amount_unparseable")
                continue
            start = offset + match.start("num")
            found.append({
                "amount_minor": minor, "currency": currency,
                "span": {"start": start, "end": offset + match.end("num")},
                "currency_span": {"start": offset + match.start(), "end": offset + match.end()},
            })
            break
    distinct = {(item["amount_minor"], item["currency"]) for item in found}
    if len(distinct) > 1:
        reasons.append("multiple_totals")
        return None
    if not found:
        reasons.append("total_not_found")
        return None
    return found[0]


def _extract_date(text: str, reasons: list[str]) -> dict | None:
    found: list[dict] = []
    for match in _DATE_RE.finditer(text):
        try:
            value = date(int(match["y"]), int(match["m"]), int(match["d"]))
        except ValueError:
            reasons.append("date_invalid")
            continue
        found.append({"occurred_on": value.isoformat(),
                      "span": {"start": match.start(), "end": match.end()}})
    if len({item["occurred_on"] for item in found}) > 1:
        reasons.append("multiple_dates")
        return None
    if not found:
        reasons.append("date_not_found")
        return None
    return found[0]


def parse_receipt_text(text: str) -> dict:
    """Return candidate fields with spans; ``needs_review`` is always True."""

    if not isinstance(text, str):
        raise ValueError("text must be a string")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    reasons: list[str] = []
    total = _extract_total(normalized, reasons)
    when = _extract_date(normalized, reasons)
    fields = {}
    if total:
        fields["amount_minor"] = {"value": total["amount_minor"], "span": total["span"]}
        fields["currency"] = {"value": total["currency"], "span": total["currency_span"]}
    if when:
        fields["occurred_on"] = {"value": when["occurred_on"], "span": when["span"]}
    return {
        "parser_version": PARSER_VERSION,
        "text": normalized,
        "fields": fields,
        "review_reasons": sorted(set(reasons)),
        "needs_review": True,
    }
