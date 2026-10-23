"""F1 accepted dataset (immutable snapshot) and owner-checked F2 snapshot loader.

``accept_dataset`` is an owner (human) action: it freezes a bounded set of already
owner-confirmed F1 entries.  ``load_snapshot`` is the read adapter for the F2
summary: owner SQL filter first, ``guard`` right after every DB await and before
any payload decryption, then digest re-verification.  Receipt candidates are not
entries and can never be members, so unconfirmed data is never aggregated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .personal_finance_contracts import (
    ACTOR_USER_ACTION,
    FinanceActor,
    FinanceActorNotAllowed,
    FinanceConflict,
    FinanceContractError,
    FinanceIdempotencyRace,
    FinanceRow,
    FinanceSource,
    compute_confirmed_digest,
)
from .personal_finance_service import _parse_idempotency_key
from .personal_async_guard import run_personal_guard
from .personal_finance_summary import (
    MAX_ROWS,
    STATE_OWNER_CONFIRMED,
    FinanceSummaryIntegrityError,
    FinanceSummaryRow,
    FinanceSummarySnapshot,
    FinanceSummaryTrustError,
)

_DIGEST_VERSION = 1


@dataclass(frozen=True)
class FinanceDatasetView:
    owner_user_id: UUID
    dataset_ref: UUID
    member_count: int
    complete: bool
    members_digest: str
    idempotency_key: str
    replayed: bool = False


def members_digest(owner_user_id: UUID, members: Sequence[tuple[UUID, str]]) -> str:
    payload = {
        "version": _DIGEST_VERSION,
        "owner_user_id": str(owner_user_id),
        "members": [[str(ref), digest] for ref, digest in sorted(members, key=lambda m: str(m[0]))],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_row_refs(row_refs: object) -> list[UUID]:
    if isinstance(row_refs, (str, bytes)) or not isinstance(row_refs, Sequence):
        raise FinanceContractError("row_refs must be a list of UUIDs")
    refs: list[UUID] = []
    for value in row_refs:
        if isinstance(value, UUID):
            refs.append(value)
        elif isinstance(value, str):
            try:
                refs.append(UUID(value))
            except ValueError as exc:
                raise FinanceContractError("row_refs must be UUIDs") from exc
        else:
            raise FinanceContractError("row_refs must be UUIDs")
    if not refs or len(refs) > MAX_ROWS:
        raise FinanceContractError(f"row_refs must hold 1..{MAX_ROWS} entries")
    if len(set(refs)) != len(refs):
        raise FinanceContractError("row_refs must be unique")
    return refs


class PersonalFinanceDatasetService:
    """Owner-filtered dataset acceptance and snapshot loading over an AsyncSession factory."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _models() -> tuple[Any, Any, Any]:
        from ..memory.models.personal_finance import (
            PersonalFinanceDataset,
            PersonalFinanceDatasetMember,
            PersonalFinanceEntry,
        )

        return PersonalFinanceEntry, PersonalFinanceDataset, PersonalFinanceDatasetMember

    async def accept_dataset(
        self,
        actor: FinanceActor,
        *,
        row_refs: Sequence[Any],
        owner_confirmed: bool,
        idempotency_key: str,
        guard: Any | None = None,
    ) -> FinanceDatasetView:
        if not isinstance(actor, FinanceActor):
            raise FinanceActorNotAllowed("actor must be an issued FinanceActor")
        if actor.kind != ACTOR_USER_ACTION:
            raise FinanceActorNotAllowed("model actors cannot accept finance datasets")
        if owner_confirmed is not True:
            raise FinanceContractError("owner_confirmed must be exactly True")
        key = _parse_idempotency_key(idempotency_key)
        refs = _parse_row_refs(row_refs)
        owner = actor.owner_user_id
        entry_model, dataset_model, member_model = self._models()

        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    entry_model.id,
                    entry_model.row_ref,
                    entry_model.content_digest,
                ).where(
                    entry_model.owner_user_id == owner,
                    entry_model.row_ref.in_(refs),
                )
            )
            await run_personal_guard(guard)
            found = {row.row_ref: (row.id, row.content_digest) for row in result}
            if set(found) != set(refs):
                raise FinanceContractError("every row_ref must be an owner-confirmed entry of this owner")
            total_result = await session.execute(
                select(func.count()).select_from(entry_model).where(entry_model.owner_user_id == owner)
            )
            await run_personal_guard(guard)
            total = total_result.scalar_one()
            digest = members_digest(owner, [(ref, found[ref][1]) for ref in refs])

            existing = await self._get_by_key(session, dataset_model, owner, key)
            await run_personal_guard(guard)
            if existing is not None:
                return self._replay_or_conflict(existing, digest)

            dataset = dataset_model(
                owner_user_id=owner,
                idempotency_key=key,
                member_count=len(refs),
                complete=total == len(refs),
                members_digest=digest,
            )
            session.add(dataset)
            try:
                await session.flush()
                await run_personal_guard(guard)
                for ref in sorted(refs, key=str):
                    session.add(
                        member_model(
                            dataset_id=dataset.id,
                            row_ref=ref,
                            owner_user_id=owner,
                            entry_id=found[ref][0],
                            content_digest=found[ref][1],
                        )
                    )
                view = FinanceDatasetView(
                    owner_user_id=owner,
                    dataset_ref=dataset.id,
                    member_count=len(refs),
                    complete=total == len(refs),
                    members_digest=digest,
                    idempotency_key=key,
                )
                await session.commit()
                await run_personal_guard(guard)
            except IntegrityError as exc:
                await session.rollback()
                if "uq_personal_finance_datasets_owner_idempotency_key" not in str(getattr(exc, "orig", exc)):
                    raise FinanceContractError("finance dataset insert failed an integrity rule") from exc
                raced = FinanceIdempotencyRace("concurrent accept collided on the owner idempotency key")
            else:
                return view
        # Lost the race: replay or conflict against the winner.
        async with self._session_factory() as session:
            existing = await self._get_by_key(session, dataset_model, owner, key)
            await run_personal_guard(guard)
        if existing is None:  # pragma: no cover - defensive
            raise raced
        return self._replay_or_conflict(existing, digest)

    async def list_datasets(
        self,
        *,
        owner_user_id: UUID,
        limit: int = 50,
        guard: Any | None = None,
    ) -> tuple[FinanceDatasetView, ...]:
        """Return a bounded owner-only list of accepted snapshot headers."""

        if not isinstance(owner_user_id, UUID) or type(limit) is not int or not 1 <= limit <= 100:
            raise FinanceContractError("invalid finance dataset list request")
        _, dataset_model, _ = self._models()
        async with self._session_factory() as session:
            result = await session.execute(
                select(dataset_model)
                .where(dataset_model.owner_user_id == owner_user_id)
                .order_by(dataset_model.created_at.desc(), dataset_model.id.desc())
                .limit(limit)
            )
            await run_personal_guard(guard)
            rows = result.scalars().all()
            return tuple(
                FinanceDatasetView(
                    owner_user_id=row.owner_user_id,
                    dataset_ref=row.id,
                    member_count=row.member_count,
                    complete=row.complete,
                    members_digest=row.members_digest,
                    idempotency_key=row.idempotency_key,
                )
                for row in rows
            )

    @staticmethod
    async def _get_by_key(session: Any, dataset_model: Any, owner: UUID, key: str) -> Any:
        result = await session.execute(
            select(dataset_model).where(
                dataset_model.owner_user_id == owner,
                dataset_model.idempotency_key == key,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _replay_or_conflict(dataset: Any, digest: str) -> FinanceDatasetView:
        if dataset.members_digest != digest:
            raise FinanceConflict("idempotency_key already recorded with a different dataset")
        return FinanceDatasetView(
            owner_user_id=dataset.owner_user_id,
            dataset_ref=dataset.id,
            member_count=dataset.member_count,
            complete=dataset.complete,
            members_digest=dataset.members_digest,
            idempotency_key=dataset.idempotency_key,
            replayed=True,
        )

    async def load_snapshot(
        self,
        *,
        owner_user_id: UUID,
        dataset_ref: UUID,
        guard: Callable[[], Any] | None = None,
    ) -> FinanceSummarySnapshot:
        """Owner SQL filter, ``guard`` after each await, decrypt, then re-verify digests."""
        if not isinstance(owner_user_id, UUID) or not isinstance(dataset_ref, UUID):
            raise FinanceSummaryTrustError("owner and dataset must be UUIDs")
        entry_model, dataset_model, member_model = self._models()
        check = guard

        async with self._session_factory() as session:
            header_result = await session.execute(
                select(dataset_model).where(
                    dataset_model.owner_user_id == owner_user_id,
                    dataset_model.id == dataset_ref,
                )
            )
            await run_personal_guard(check)
            header = header_result.scalar_one_or_none()
            if header is None:
                raise FinanceSummaryTrustError("accepted dataset not available for this owner")
            member_result = await session.execute(
                select(member_model, entry_model)
                .join(
                    entry_model,
                    (entry_model.owner_user_id == member_model.owner_user_id)
                    & (entry_model.id == member_model.entry_id),
                )
                .where(
                    member_model.owner_user_id == owner_user_id,
                    member_model.dataset_id == dataset_ref,
                )
                .order_by(member_model.row_ref)
                .limit(MAX_ROWS + 1)
            )
            await run_personal_guard(check)
            pairs = list(member_result.all())
            # Payload decryption happens only below, after the guard re-check.
            if len(pairs) != header.member_count or len(pairs) > MAX_ROWS:
                raise FinanceSummaryIntegrityError("dataset members do not match the frozen member_count")
            if (
                members_digest(owner_user_id, [(m.row_ref, m.content_digest) for m, _ in pairs])
                != header.members_digest
            ):
                raise FinanceSummaryIntegrityError("dataset members do not match the frozen digest")
            rows = tuple(self._summary_row(owner_user_id, member, entry) for member, entry in pairs)
            return FinanceSummarySnapshot(
                owner_user_id=owner_user_id,
                dataset_ref=dataset_ref,
                immutable=True,
                complete=bool(header.complete),
                rows=rows,
            )

    @staticmethod
    def _summary_row(owner: UUID, member: Any, entry: Any) -> FinanceSummaryRow:
        if (
            entry.owner_user_id != owner
            or entry.row_ref != member.row_ref
            or entry.content_digest != member.content_digest
            or entry.confirmed_by_kind != ACTOR_USER_ACTION
        ):
            raise FinanceSummaryIntegrityError("dataset member does not match its confirmed entry")
        payload = entry.payload or {}
        row = FinanceRow.from_mapping(payload.get("row") or {})
        source_raw = payload.get("source")
        source = None if source_raw is None else FinanceSource.from_mapping(source_raw)
        if compute_confirmed_digest(owner_user_id=owner, row=row, source=source) != entry.content_digest:
            raise FinanceSummaryIntegrityError("entry content does not match its confirmed digest")
        return FinanceSummaryRow(
            row_ref=row.row_ref,
            occurred_on=row.occurred_on.isoformat(),
            kind=row.kind,
            amount_minor=row.amount_minor,
            currency=row.currency,
            confirmation_state=STATE_OWNER_CONFIRMED,
        )
