"""F1 personal finance ENTRY service (owner-confirmed append only).

Trusted caller convention: the actor is issued by the contracts factories and
is *not* an authentication proof.  The service still refuses model actors and
any ``owner_confirmed`` value other than the exact boolean ``True``.

Only append exists here: no update, delete, summary, receipt, or model
auto-accept path.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .personal_finance_contracts import (
    ACTOR_USER_ACTION,
    FinanceActor,
    FinanceActorNotAllowed,
    FinanceConflict,
    FinanceContractError,
    FinanceDigestMismatch,
    FinanceDuplicateRowRef,
    FinanceEntryView,
    FinanceIdempotencyRace,
    FinanceRow,
    FinanceSource,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    compute_confirmed_digest,
)


def _parse_idempotency_key(value: object) -> str:
    if not isinstance(value, str):
        raise FinanceContractError("idempotency_key must be a string")
    key = value.strip()
    if not key or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise FinanceContractError(
            f"idempotency_key must be 1..{MAX_IDEMPOTENCY_KEY_LENGTH} characters"
        )
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in key):
        raise FinanceContractError("idempotency_key must not carry control characters")
    return key


class PersonalFinanceRepository(Protocol):
    """Owner-filtered storage contract used by the service."""

    async def get_by_idempotency_key(
        self,
        *,
        owner_user_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None: ...

    async def get_by_row_ref(
        self,
        *,
        owner_user_id: UUID,
        row_ref: UUID,
        guard: Callable[[], None] | None = None,
    ) -> Mapping[str, Any] | None: ...

    async def insert(
        self,
        *,
        owner_user_id: UUID,
        idempotency_key: str,
        row_ref: UUID,
        content_digest: str,
        confirmed_by_kind: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


def _view_from_record(record: Mapping[str, Any], *, replayed: bool) -> FinanceEntryView:
    payload = record.get("payload") or {}
    row = payload.get("row") or {}
    source = payload.get("source")
    return FinanceEntryView(
        owner_user_id=record["owner_user_id"],
        row_ref=record["row_ref"],
        occurred_on=row["occurred_on"],
        kind=row["kind"],
        amount_minor=row["amount_minor"],
        currency=row["currency"],
        account_alias=row["account_alias"],
        category=row.get("category"),
        source=None if source is None else dict(source),
        content_digest=record["content_digest"],
        idempotency_key=record["idempotency_key"],
        confirmed_by_kind=record["confirmed_by_kind"],
        replayed=replayed,
    )


class PersonalFinanceService:
    """Bounded F1 entry service: append confirmed entry, nothing else."""

    def __init__(self, repository: PersonalFinanceRepository) -> None:
        self._repository = repository

    async def append_confirmed_entry(
        self,
        actor: FinanceActor,
        *,
        row: Mapping[str, Any],
        source: Mapping[str, Any] | None = None,
        confirmed_digest: str,
        owner_confirmed: bool,
        idempotency_key: str,
    ) -> FinanceEntryView:
        if not isinstance(actor, FinanceActor):
            raise FinanceActorNotAllowed("actor must be an issued FinanceActor")
        if actor.kind != ACTOR_USER_ACTION:
            raise FinanceActorNotAllowed("model actors cannot confirm finance entries")
        if owner_confirmed is not True:
            raise FinanceContractError("owner_confirmed must be exactly True")

        key = _parse_idempotency_key(idempotency_key)
        entry_row = FinanceRow.from_mapping(row)
        entry_source = None if source is None else FinanceSource.from_mapping(source)

        expected_digest = compute_confirmed_digest(
            owner_user_id=actor.owner_user_id,
            row=entry_row,
            source=entry_source,
        )
        if not isinstance(confirmed_digest, str):
            raise FinanceDigestMismatch("confirmed_digest must be a SHA256 hex digest")
        if confirmed_digest.strip().lower() != expected_digest:
            raise FinanceDigestMismatch(
                "confirmed_digest does not bind the owner and normalized content"
            )

        payload = {
            "row": entry_row.to_canonical(),
            "source": None if entry_source is None else entry_source.to_canonical(),
        }

        existing = await self._repository.get_by_idempotency_key(
            owner_user_id=actor.owner_user_id,
            idempotency_key=key,
        )
        if existing is not None:
            return self._replay_or_conflict(existing, content_digest=expected_digest)

        duplicate = await self._repository.get_by_row_ref(
            owner_user_id=actor.owner_user_id,
            row_ref=entry_row.row_ref,
        )
        if duplicate is not None:
            raise FinanceDuplicateRowRef(
                "row_ref is already stored for this owner by another operation"
            )

        try:
            record = await self._repository.insert(
                owner_user_id=actor.owner_user_id,
                idempotency_key=key,
                row_ref=entry_row.row_ref,
                content_digest=expected_digest,
                confirmed_by_kind=actor.kind,
                payload=payload,
            )
        except FinanceIdempotencyRace:
            raced = await self._repository.get_by_idempotency_key(
                owner_user_id=actor.owner_user_id,
                idempotency_key=key,
            )
            if raced is None:  # pragma: no cover - defensive
                raise
            return self._replay_or_conflict(raced, content_digest=expected_digest)

        return _view_from_record(record, replayed=False)

    @staticmethod
    def _replay_or_conflict(
        record: Mapping[str, Any],
        *,
        content_digest: str,
    ) -> FinanceEntryView:
        if record.get("content_digest") != content_digest:
            raise FinanceConflict(
                "idempotency_key already recorded with different content"
            )
        return _view_from_record(record, replayed=True)


def _record_from_entry(entry: Any) -> dict[str, Any]:
    return {
        "entry_id": entry.id,
        "owner_user_id": entry.owner_user_id,
        "row_ref": entry.row_ref,
        "idempotency_key": entry.idempotency_key,
        "content_digest": entry.content_digest,
        "confirmed_by_kind": entry.confirmed_by_kind,
        "payload": entry.payload,
    }


def _translate_integrity_error(exc: IntegrityError) -> FinanceContractError:
    detail = str(getattr(exc, "orig", exc))
    if "uq_personal_finance_entries_owner_row_ref" in detail:
        return FinanceDuplicateRowRef(
            "row_ref is already stored for this owner by another operation"
        )
    if "uq_personal_finance_entries_owner_idempotency_key" in detail:
        return FinanceIdempotencyRace(
            "concurrent append collided on the owner idempotency key"
        )
    return FinanceContractError("finance entry insert failed a uniqueness rule")


class SqlPersonalFinanceRepository:
    """AsyncSession-factory repository with owner-filtered reads/inserts."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _model() -> Any:
        from ..memory.models.personal_finance import PersonalFinanceEntry

        return PersonalFinanceEntry

    async def get_by_idempotency_key(
        self,
        *,
        owner_user_id: UUID,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        model = self._model()
        async with self._session_factory() as session:
            result = await session.execute(
                select(model).where(
                    model.owner_user_id == owner_user_id,
                    model.idempotency_key == idempotency_key,
                )
            )
            entry = result.scalar_one_or_none()
            return None if entry is None else _record_from_entry(entry)

    async def get_by_row_ref(
        self,
        *,
        owner_user_id: UUID,
        row_ref: UUID,
        guard: Callable[[], None] | None = None,
    ) -> dict[str, Any] | None:
        """``guard`` (trusted read hook) runs right after the DB await, before payload decryption."""
        model = self._model()
        async with self._session_factory() as session:
            result = await session.execute(
                select(model).where(
                    model.owner_user_id == owner_user_id,
                    model.row_ref == row_ref,
                )
            )
            if guard is not None:
                guard()
            entry = result.scalar_one_or_none()
            return None if entry is None else _record_from_entry(entry)

    async def insert(
        self,
        *,
        owner_user_id: UUID,
        idempotency_key: str,
        row_ref: UUID,
        content_digest: str,
        confirmed_by_kind: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        model = self._model()
        entry = model(
            owner_user_id=owner_user_id,
            row_ref=row_ref,
            idempotency_key=idempotency_key,
            content_digest=content_digest,
            confirmed_by_kind=confirmed_by_kind,
        )
        entry.payload = dict(payload)
        record = {
            "entry_id": entry.id,
            "owner_user_id": owner_user_id,
            "row_ref": row_ref,
            "idempotency_key": idempotency_key,
            "content_digest": content_digest,
            "confirmed_by_kind": confirmed_by_kind,
            "payload": dict(payload),
        }
        async with self._session_factory() as session:
            session.add(entry)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise _translate_integrity_error(exc) from exc
        return record
