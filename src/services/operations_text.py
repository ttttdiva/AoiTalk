"""Text normalization against the unchanged Operations persistence contracts."""

from __future__ import annotations

OPERATIONS_TEXT_BYTE_LIMITS = {
    "fit": 32,
    "summary": 32_000,
    "message": 64_000,
    "currency": 16,
    "delivery_estimate": 255,
}
EVALUATION_COLLECTION_MAX_BYTES = 16_000


def bounded_operation_text(value: str, field: str) -> str:
    """Keep a Unicode prefix within the field's UTF-8 limit, without padding.

    Callers retain responsibility for required/nonempty validation and for
    holding any decision whose substantive text had to be shortened.
    """
    encoded = value.encode("utf-8")
    limit = OPERATIONS_TEXT_BYTE_LIMITS[field]
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def model_text(value: object) -> str | None:
    """Accept actual, valid Unicode model text; never stringify structures."""
    if not isinstance(value, str) or "\0" in value:
        return None
    try:
        value.encode("utf-8")
    except UnicodeError:
        return None
    return value.strip()


def model_text_list(value: object) -> bool:
    return isinstance(value, list) and all(model_text(item) is not None for item in value)
