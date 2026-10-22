"""F1 personal finance ENTRY contracts.

Scope: owner-confirmed finance ENTRY (income / expense) append only.
No receipts, summaries, UI, monetary parser/formatter, automatic duplicate
acceptance, inferred rates, update, or delete.

``FinanceActor`` issuance is a *trusted caller convention*, not an
authentication proof.  The factories keep model actors and owner actors
separable so a model-facing caller cannot claim owner confirmation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

ACTOR_USER_ACTION = "user_action"
ACTOR_MODEL = "model"
ACTOR_KINDS = frozenset({ACTOR_USER_ACTION, ACTOR_MODEL})

MAX_AMOUNT_MINOR = 9_007_199_254_740_991
MAX_SOURCE_BYTE_SIZE = 10_485_760
MAX_CATEGORY_LENGTH = 80
MAX_ACCOUNT_ALIAS_LENGTH = 64
MAX_MIME_TYPE_LENGTH = 128
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_SOURCE_SHA256_LENGTH = 64

SUPPORTED_ENTRY_KINDS = frozenset({"income", "expense"})
UNSUPPORTED_ENTRY_KINDS = frozenset({"balance", "transfer", "adjustment"})

_ROW_FIELDS = (
    "row_ref",
    "occurred_on",
    "kind",
    "amount_minor",
    "currency",
    "account_alias",
    "category",
)
_SOURCE_FIELDS = (
    "source_ref",
    "source_sha256",
    "mime_type",
    "byte_size",
)
_FORBIDDEN_ROW_FIELDS = frozenset(
    {
        "actor",
        "actor_owner_user_id",
        "confirmed_digest",
        "content_digest",
        "idempotency_key",
        "owner",
        "owner_id",
        "owner_user_id",
        "payload",
        "session",
        "session_id",
        "tenant_id",
        "user_id",
    }
)
_FORBIDDEN_SOURCE_FIELDS = frozenset(
    {
        "actor",
        "actor_owner_user_id",
        "bytes_",
        "content",
        "data",
        "file_path",
        "owner",
        "owner_id",
        "owner_user_id",
        "path",
        "payload",
        "session",
        "session_id",
        "uri",
        "url",
    }
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FinanceContractError(ValueError):
    """Bounded F1 contract rejection."""


class FinanceActorNotAllowed(FinanceContractError):
    """Actor kind is not allowed to append an owner-confirmed entry."""


class FinanceDigestMismatch(FinanceContractError):
    """Caller supplied digest does not bind the normalized entry content."""


class FinanceConflict(FinanceContractError):
    """Same owner + idempotency key already holds different content."""


class FinanceDuplicateRowRef(FinanceContractError):
    """Same owner already stored the row_ref with a different operation."""


class FinanceIdempotencyRace(FinanceContractError):
    """Concurrent insert collided on the owner + idempotency key unique index."""


_ISSUER_SENTINEL = object()


@dataclass(frozen=True)
class FinanceActor:
    """Frozen actor descriptor; construct through the module factories only."""

    kind: str
    owner_user_id: UUID
    _issuer: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer is not _ISSUER_SENTINEL:
            raise FinanceActorNotAllowed(
                "FinanceActor must be issued by finance_owner_actor or "
                "finance_model_actor"
            )
        if self.kind not in ACTOR_KINDS:
            raise FinanceActorNotAllowed(f"unsupported actor kind: {self.kind!r}")
        if not isinstance(self.owner_user_id, UUID):
            raise FinanceContractError("actor owner_user_id must be a UUID")

    @property
    def is_owner_action(self) -> bool:
        return self.kind == ACTOR_USER_ACTION

    @property
    def is_model(self) -> bool:
        return self.kind == ACTOR_MODEL


def finance_owner_actor(owner_user_id: UUID) -> FinanceActor:
    """Issue an owner (human action) actor for one owner scope."""

    return FinanceActor(
        kind=ACTOR_USER_ACTION,
        owner_user_id=owner_user_id,
        _issuer=_ISSUER_SENTINEL,
    )


def finance_model_actor(owner_user_id: UUID) -> FinanceActor:
    """Issue a model actor; it can never append a confirmed entry."""

    return FinanceActor(
        kind=ACTOR_MODEL,
        owner_user_id=owner_user_id,
        _issuer=_ISSUER_SENTINEL,
    )


def _normalized_key(key: object) -> str:
    return str(key).strip().lower().replace("-", "_")


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FinanceContractError(f"{label} must be a mapping")
    return value


def _reject_unknown_fields(
    mapping: Mapping[str, Any],
    *,
    allowed: tuple[str, ...],
    forbidden: frozenset[str],
    label: str,
) -> None:
    for raw_key in mapping:
        key = _normalized_key(raw_key)
        if key in forbidden:
            raise FinanceContractError(f"{label} must not carry field {key!r}")
        if key not in allowed:
            raise FinanceContractError(f"{label} has unsupported field {key!r}")


def _parse_uuid(value: object, label: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise FinanceContractError(f"{label} must be a UUID") from exc
    raise FinanceContractError(f"{label} must be a UUID")


def _parse_occurred_on(value: object) -> date:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise FinanceContractError("occurred_on must be an explicit YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FinanceContractError("occurred_on must be a valid calendar date") from exc


def _parse_amount_minor(value: object) -> int:
    if type(value) is not int:  # bool and float are excluded on purpose
        raise FinanceContractError("amount_minor must be an integer minor amount")
    if value < 0 or value > MAX_AMOUNT_MINOR:
        raise FinanceContractError(
            f"amount_minor must be within 0..{MAX_AMOUNT_MINOR}"
        )
    return value


def _parse_currency(value: object) -> str:
    if not isinstance(value, str):
        raise FinanceContractError("currency must be a 3-letter uppercase code")
    if len(value) != 3 or not value.isalpha() or not value.isupper():
        raise FinanceContractError("currency must be a 3-letter uppercase code")
    return value


def _parse_alias(value: object) -> str:
    if not isinstance(value, str):
        raise FinanceContractError("account_alias must be a string")
    alias = value.strip()
    if not alias or len(alias) > MAX_ACCOUNT_ALIAS_LENGTH:
        raise FinanceContractError(
            f"account_alias must be 1..{MAX_ACCOUNT_ALIAS_LENGTH} characters"
        )
    return alias


def _parse_category(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FinanceContractError("category must be a string or null")
    category = value.strip()
    if not category:
        return None
    if len(category) > MAX_CATEGORY_LENGTH:
        raise FinanceContractError(
            f"category must be at most {MAX_CATEGORY_LENGTH} characters"
        )
    return category


@dataclass(frozen=True)
class FinanceRow:
    """Normalized finance ENTRY row (private content, integer fixtures only)."""

    row_ref: UUID
    occurred_on: date
    kind: str
    amount_minor: int
    currency: str
    account_alias: str
    category: str | None = None

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "FinanceRow":
        mapping = _require_mapping(row, "row")
        _reject_unknown_fields(
            mapping,
            allowed=_ROW_FIELDS,
            forbidden=_FORBIDDEN_ROW_FIELDS,
            label="row",
        )
        required = [name for name in _ROW_FIELDS if name != "category"]
        missing = [name for name in required if name not in mapping]
        if missing:
            raise FinanceContractError(
                "row is missing required field(s): " + ", ".join(sorted(missing))
            )
        kind = mapping["kind"]
        if not isinstance(kind, str):
            raise FinanceContractError("kind must be a string")
        normalized_kind = kind.strip().lower()
        if normalized_kind in UNSUPPORTED_ENTRY_KINDS:
            raise FinanceContractError(
                f"kind {normalized_kind!r} is not supported by the F1 entry slice"
            )
        if normalized_kind not in SUPPORTED_ENTRY_KINDS:
            raise FinanceContractError("kind must be income or expense")
        return cls(
            row_ref=_parse_uuid(mapping["row_ref"], "row_ref"),
            occurred_on=_parse_occurred_on(mapping["occurred_on"]),
            kind=normalized_kind,
            amount_minor=_parse_amount_minor(mapping["amount_minor"]),
            currency=_parse_currency(mapping["currency"]),
            account_alias=_parse_alias(mapping["account_alias"]),
            category=_parse_category(mapping.get("category")),
        )

    def to_canonical(self) -> dict[str, Any]:
        return {
            "row_ref": str(self.row_ref),
            "occurred_on": self.occurred_on.isoformat(),
            "kind": self.kind,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "account_alias": self.account_alias,
            "category": self.category,
        }


@dataclass(frozen=True)
class FinanceSource:
    """Optional private source descriptor; no paths, URLs, or content bytes."""

    source_ref: UUID
    source_sha256: str
    mime_type: str
    byte_size: int

    @classmethod
    def from_mapping(cls, source: Mapping[str, Any]) -> "FinanceSource":
        mapping = _require_mapping(source, "source")
        _reject_unknown_fields(
            mapping,
            allowed=_SOURCE_FIELDS,
            forbidden=_FORBIDDEN_SOURCE_FIELDS,
            label="source",
        )
        missing = [name for name in _SOURCE_FIELDS if name not in mapping]
        if missing:
            raise FinanceContractError(
                "source is missing required field(s): " + ", ".join(sorted(missing))
            )
        sha256 = mapping["source_sha256"]
        if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
            raise FinanceContractError(
                "source_sha256 must be 64 lowercase hex characters"
            )
        mime_type = mapping["mime_type"]
        if not isinstance(mime_type, str):
            raise FinanceContractError("mime_type must be a string")
        normalized_mime = mime_type.strip()
        if (
            not normalized_mime
            or len(normalized_mime) > MAX_MIME_TYPE_LENGTH
            or any(ch in normalized_mime for ch in "\r\n\t ")
        ):
            raise FinanceContractError(
                f"mime_type must be 1..{MAX_MIME_TYPE_LENGTH} characters"
            )
        byte_size = mapping["byte_size"]
        if type(byte_size) is not int:
            raise FinanceContractError("byte_size must be an integer")
        if byte_size < 1 or byte_size > MAX_SOURCE_BYTE_SIZE:
            raise FinanceContractError(
                f"byte_size must be within 1..{MAX_SOURCE_BYTE_SIZE}"
            )
        return cls(
            source_ref=_parse_uuid(mapping["source_ref"], "source_ref"),
            source_sha256=sha256,
            mime_type=normalized_mime,
            byte_size=byte_size,
        )

    def to_canonical(self) -> dict[str, Any]:
        return {
            "source_ref": str(self.source_ref),
            "source_sha256": self.source_sha256,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
        }


@dataclass(frozen=True)
class FinanceEntryView:
    """Bounded read view of one appended entry."""

    owner_user_id: UUID
    row_ref: UUID
    occurred_on: str
    kind: str
    amount_minor: int
    currency: str
    account_alias: str
    category: str | None
    source: dict[str, Any] | None
    content_digest: str
    idempotency_key: str
    confirmed_by_kind: str
    replayed: bool = False


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_confirmed_digest(
    *,
    owner_user_id: UUID,
    row: FinanceRow,
    source: FinanceSource | None = None,
) -> str:
    """SHA256 binding actor owner UUID + normalized row/source content."""

    if not isinstance(owner_user_id, UUID):
        raise FinanceContractError("digest owner_user_id must be a UUID")
    if not isinstance(row, FinanceRow):
        raise FinanceContractError("digest row must be a FinanceRow")
    if source is not None and not isinstance(source, FinanceSource):
        raise FinanceContractError("digest source must be a FinanceSource or None")
    payload = {
        "version": 1,
        "actor_owner_user_id": str(owner_user_id),
        "row": row.to_canonical(),
        "source": None if source is None else source.to_canonical(),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
