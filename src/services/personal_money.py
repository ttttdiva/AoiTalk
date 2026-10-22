"""Personal money normalization: strict minor-unit parse/format for JPY/USD/EUR/GBP."""

CURRENCY_MINOR_DIGITS = {"JPY": 0, "USD": 2, "EUR": 2, "GBP": 2}

MAX_MAJOR_UNITS = 10**12
_MAX_INPUT_CHARS = 32


def _validate_currency(currency: str) -> int:
    if not isinstance(currency, str):
        raise ValueError("currency must be a str")
    digits = CURRENCY_MINOR_DIGITS.get(currency)
    if digits is None:
        raise ValueError(f"unsupported currency: {currency!r}")
    return digits


def parse_minor_units(amount: str, currency: str) -> int:
    """Parse an unsigned plain decimal string into minor units. Strict; no stripping."""
    digits = _validate_currency(currency)
    if not isinstance(amount, str) or len(amount) > _MAX_INPUT_CHARS:
        raise ValueError("amount must be a short plain str")
    if not amount.isascii():
        raise ValueError("amount must be ASCII")
    parts = amount.split(".")
    if len(parts) > 2:
        raise ValueError("invalid amount: separators/exponent not allowed")
    major, _, fraction = parts[0], ("" if len(parts) == 1 else "."), parts[-1]
    if not major.isdigit():
        raise ValueError("invalid amount: missing major digits")
    if len(major) > 1 and major[0] == "0":
        raise ValueError("invalid amount: leading zeros")
    if len(parts) == 2:
        if len(fraction) < 1 or len(fraction) > digits:
            raise ValueError("invalid amount: fraction digits out of range")
        if not fraction.isdigit():
            raise ValueError("invalid amount: fraction must be ASCII digits")
    else:
        fraction = ""
    if len(major) >= len(str(MAX_MAJOR_UNITS)):
        raise ValueError("invalid amount: major out of bounds")
    return int(major) * 10**digits + (int(fraction.ljust(digits, "0")) if fraction else 0)


def format_minor_units(minor_units: int, currency: str) -> str:
    """Render minor units as a plain decimal string. int only; no rounding."""
    digits = _validate_currency(currency)
    if isinstance(minor_units, bool) or not isinstance(minor_units, int):
        raise ValueError("minor_units must be an int")
    if minor_units < 0 or minor_units >= MAX_MAJOR_UNITS * 10**digits:
        raise ValueError("minor_units out of bounds")
    if digits == 0:
        return str(minor_units)
    major, fraction = divmod(minor_units, 10**digits)
    return f"{major}.{fraction:0{digits}d}"
