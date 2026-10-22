"""ユーザー個人の追記専用台帳（期限・在庫）のサービス。

境界:
- 行は追記のみ。訂正は ``supersedes_entry_id`` で前の行を指す新しい行。
- 現在値は保存せず、行の集合から ``derive_state`` で決定的に導出する。
- 期限の「確認済み」「完了」は実世界の事実の主張なので、``user_action`` の Actor だけが
  追記できる。モデルが渡す enum や source 値は根拠にならない。

信頼境界（重要）:
- DB の ``actor_kind`` 列と CHECK 制約は「保存された行の整合」を守るだけで、上流で
  利用者が認証されたことや確認操作が行われたことの証明にはならない。信頼の根拠は
  アプリケーション層の呼出し規約にある。
- ``LedgerActor`` は直接生成できない（``agent_actor`` / ``agent_actor_from_turn_context`` /
  ``user_action_actor`` だけが発行する）。``append_entry`` / ``list_entries`` / ``current_state``
  は owner や actor_kind を引数で受けない。owner は常に Actor の user_id になる。
- 将来の HTTP/ツール adapter は、リクエスト本文・クエリ・ツール引数から owner や actor_kind を
  受け取ってはならない。Agent/ツール経路は ``agent_actor_from_turn_context()`` だけを使い、
  ``user_action_actor`` は認証済みセッションから得た user_id を渡すユーザー操作用 HTTP
  ルートだけが呼ぶ（Agent/LLM 層から参照しないことはテストで検査する）。
- 日付は利用者が示した値をそのまま保存する。法定期限や周期を算出・補完しない。
- 外部送信、取引、調達、武器の使用に当たる機能は持たない。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import aliased
from sqlalchemy.exc import IntegrityError

from ..memory.models.personal_ledger import (
    PERSONAL_LEDGER_ENTRY_TYPES,
    PERSONAL_LEDGER_KINDS,
    PERSONAL_LEDGER_USER_ONLY_ENTRY_TYPES,
    PersonalLedgerEntry,
)

PAYLOAD_SCHEMA_VERSION = 1
MAX_STATE_ENTRIES = 5000
MAX_QUANTITY = 10**9

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_UNIT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

_DEADLINE_TYPES = {"deadline_candidate", "deadline_verification", "deadline_completion"}
_INVENTORY_TYPES = {"opening_balance", "purchase", "consumption"}


class LedgerError(Exception):
    """台帳サービスの基底エラー。"""

    code = "ledger_error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class LedgerValidationError(LedgerError):
    code = "ledger_validation"


class LedgerNotFoundError(LedgerError):
    """存在しない、または他の利用者の行（区別しない）。"""

    code = "ledger_not_found"


class LedgerConflictError(LedgerError):
    code = "ledger_conflict"


class LedgerPermissionError(LedgerError):
    code = "ledger_permission"


_ISSUER = object()


@dataclass(frozen=True)
class LedgerActor:
    """追記・参照の主体。owner は常に actor の user_id（引数で上書きできない）。

    直接生成は拒否する。発行できるのは下の factory 関数だけ。Python では完全には封じられないため、
    これは誤用防止であり、認証の証明ではない（モジュール docstring の信頼境界を参照）。
    """

    kind: str
    user_id: UUID
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer is not _ISSUER:
            raise LedgerPermissionError("LedgerActor must be issued by a factory function")


def agent_actor(user_id: Any) -> LedgerActor:
    """モデル/Agent 由来の主体。確認済み・完了は作れない。

    user_id は認証済みコンテキストから得た値に限る。通常は ``agent_actor_from_turn_context`` を使う。
    """

    return LedgerActor("agent", _uuid(user_id, "user_id"), _ISSUER)


def agent_actor_from_turn_context() -> LedgerActor:
    """現在のターンの認証済み利用者から Agent 主体を作る。ツール引数から owner を取らない。"""

    from .turn_context import get_turn_context

    user_id = get_turn_context().user_id
    if not user_id:
        raise LedgerPermissionError("an authenticated user context is required")
    return agent_actor(user_id)


def user_action_actor(user_id: Any) -> LedgerActor:
    """認証済みのユーザー操作由来の主体。

    呼べるのは、認証済みセッションから user_id を得て、利用者が内容を確認した操作を処理する
    ユーザー向け HTTP ルートだけ。リクエスト本文やツール引数の値を user_id に使ってはならない。
    """

    return LedgerActor("user_action", _uuid(user_id, "user_id"), _ISSUER)


@dataclass(frozen=True)
class LedgerEntryView:
    id: UUID
    seq: int
    owner_user_id: UUID
    ledger: str
    entry_type: str
    subject_key: str
    actor_kind: str
    supersedes_entry_id: UUID | None
    refers_to_entry_id: UUID | None
    idempotency_key: str
    payload: Mapping[str, Any]
    recorded_at: datetime | None = None


@dataclass(frozen=True)
class LedgerPage:
    """新しい順の1ページ。superseded は全履歴に対してサーバーが判定した結果。"""

    entries: tuple[LedgerEntryView, ...]
    superseded_ids: frozenset[UUID]
    # 次ページ（より古い行）を読むためのカーソル。None なら末尾。
    next_before_seq: int | None


MAX_PAGE_SIZE = 200


@dataclass(frozen=True)
class AppendResult:
    entry: LedgerEntryView
    replayed: bool


@dataclass(frozen=True)
class DeadlineState:
    subject_key: str
    candidate_entry_id: UUID
    due_date: str
    label: str | None
    status: str  # "unverified" | "verified"
    verified_entry_id: UUID | None
    completed_on: str | None
    needs_review: tuple[str, ...] = ()
    # 候補を入力した主体（"agent" = Agent の観測、"user_action" = 本人入力）。確認済みとは別の情報。
    candidate_actor_kind: str = ""
    # 利用者が画面で見た内容の指紋。確認操作はこれを返す。
    confirm_digest: str = ""
    # 現在有効な完了行（訂正・取消しの対象）。
    completion_entry_id: UUID | None = None


@dataclass(frozen=True)
class InventoryItemState:
    subject_key: str
    balances: tuple[tuple[str, int], ...]  # (unit, balance) を unit 昇順で
    needs_review: tuple[str, ...] = ()


@dataclass(frozen=True)
class LedgerState:
    deadlines: tuple[DeadlineState, ...] = ()
    inventory: tuple[InventoryItemState, ...] = field(default_factory=tuple)


def _uuid(value: Any, label: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise LedgerValidationError(f"invalid {label}") from None


def _optional_uuid(value: Any, label: str) -> UUID | None:
    return None if value is None else _uuid(value, label)


def _date_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 10:
        raise LedgerValidationError(f"{label} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise LedgerValidationError(f"{label} must be a valid calendar date") from None


def _optional_text(value: Any, label: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise LedgerValidationError(f"invalid {label}")
    return value.strip()


def _only_keys(payload: Mapping[str, Any], allowed: set[str]) -> None:
    extra = set(payload) - allowed - {"schema"}
    if extra:
        raise LedgerValidationError("unsupported payload field")


def candidate_digest(*, entry_id: UUID, subject_key: str, due_date: str) -> str:
    """利用者が確認した内容（どの候補・どの日付か）の指紋。"""

    canonical = json.dumps(
        {"entry_id": str(entry_id), "subject_key": subject_key, "due_date": due_date},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_payload(entry_type: str, payload: Any) -> dict[str, Any]:
    """種別ごとの許可項目だけを通す。空ペイロードは作らない（暗号化対象にするため schema を付ける）。"""

    if not isinstance(payload, Mapping):
        raise LedgerValidationError("payload must be an object")
    if payload.get("schema", PAYLOAD_SCHEMA_VERSION) != PAYLOAD_SCHEMA_VERSION:
        raise LedgerValidationError("unsupported payload schema")
    out: dict[str, Any] = {"schema": PAYLOAD_SCHEMA_VERSION}
    if entry_type == "deadline_candidate":
        _only_keys(payload, {"due_date", "label"})
        out["due_date"] = _date_text(payload.get("due_date"), "due_date")
        label = _optional_text(payload.get("label"), "label", 120)
        if label is not None:
            out["label"] = label
    elif entry_type == "deadline_verification":
        _only_keys(payload, {"confirmed_digest"})
        digest = payload.get("confirmed_digest")
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise LedgerValidationError("invalid confirmed_digest")
        out["confirmed_digest"] = digest
    elif entry_type == "deadline_completion":
        _only_keys(payload, {"completed_on"})
        out["completed_on"] = _date_text(payload.get("completed_on"), "completed_on")
    elif entry_type in _INVENTORY_TYPES:
        _only_keys(payload, {"quantity", "unit", "occurred_on", "note"})
        quantity = payload.get("quantity")
        # 初期残数は 0 を許す（在庫が無い状態を記録できる）。購入・消費は 1 以上。
        minimum = 0 if entry_type == "opening_balance" else 1
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not minimum <= quantity <= MAX_QUANTITY:
            raise LedgerValidationError(
                "quantity must be a non-negative integer" if minimum == 0 else "quantity must be a positive integer"
            )
        unit = payload.get("unit")
        if not isinstance(unit, str) or not _UNIT_RE.fullmatch(unit):
            raise LedgerValidationError("invalid unit")
        out.update(quantity=quantity, unit=unit, occurred_on=_date_text(payload.get("occurred_on"), "occurred_on"))
        note = _optional_text(payload.get("note"), "note", 200)
        if note is not None:
            out["note"] = note
    elif entry_type == "void":
        _only_keys(payload, {"reason"})
        reason = _optional_text(payload.get("reason"), "reason", 200)
        if reason is not None:
            out["reason"] = reason
    else:
        raise LedgerValidationError("unsupported entry_type")
    return out


def _kind_of(entry_type: str) -> str | None:
    if entry_type in _DEADLINE_TYPES:
        return "deadline"
    if entry_type in _INVENTORY_TYPES:
        return "inventory"
    return None  # void はどちらの台帳にも付く


def derive_state(entries: Iterable[LedgerEntryView]) -> LedgerState:
    """行の集合から現在値を導出する（入力順に依存しない純関数）。"""

    rows = sorted(entries, key=lambda e: e.seq)
    superseded = {e.supersedes_entry_id for e in rows if e.supersedes_entry_id is not None}
    effective = [e for e in rows if e.id not in superseded and e.entry_type != "void"]

    deadlines: list[DeadlineState] = []
    candidates: dict[str, list[LedgerEntryView]] = {}
    for e in effective:
        if e.ledger == "deadline" and e.entry_type == "deadline_candidate":
            candidates.setdefault(e.subject_key, []).append(e)
    for subject in sorted(candidates):
        group = candidates[subject]
        cand = group[-1]
        review = ("multiple_effective_candidates",) if len(group) > 1 else ()
        due = str(cand.payload.get("due_date"))
        expected = candidate_digest(entry_id=cand.id, subject_key=subject, due_date=due)
        verified = next(
            (
                v
                for v in effective
                if v.entry_type == "deadline_verification"
                and v.refers_to_entry_id == cand.id
                and v.payload.get("confirmed_digest") == expected
            ),
            None,
        )
        done = [c for c in effective if c.entry_type == "deadline_completion" and c.refers_to_entry_id == cand.id]
        deadlines.append(
            DeadlineState(
                subject_key=subject,
                candidate_entry_id=cand.id,
                due_date=due,
                label=cand.payload.get("label"),
                status="verified" if verified else "unverified",
                verified_entry_id=verified.id if verified else None,
                completed_on=done[-1].payload.get("completed_on") if done else None,
                needs_review=review,
                candidate_actor_kind=cand.actor_kind,
                confirm_digest=expected,
                completion_entry_id=done[-1].id if done else None,
            )
        )

    items: dict[str, list[LedgerEntryView]] = {}
    for e in effective:
        if e.ledger == "inventory" and e.entry_type in _INVENTORY_TYPES:
            items.setdefault(e.subject_key, []).append(e)
    inventory: list[InventoryItemState] = []
    for subject in sorted(items):
        balances: dict[str, int] = {}
        openings: dict[str, int] = {}
        for e in items[subject]:
            unit = str(e.payload["unit"])
            qty = int(e.payload["quantity"])
            sign = -1 if e.entry_type == "consumption" else 1
            balances[unit] = balances.get(unit, 0) + sign * qty
            if e.entry_type == "opening_balance":
                openings[unit] = openings.get(unit, 0) + 1
        review: list[str] = []
        if len(balances) > 1:
            review.append("multiple_units")
        if any(v < 0 for v in balances.values()):
            review.append("negative_balance")
        if any(v > 1 for v in openings.values()):
            review.append("multiple_opening_balances")
        inventory.append(
            InventoryItemState(
                subject_key=subject,
                balances=tuple(sorted(balances.items())),
                needs_review=tuple(review),
            )
        )
    return LedgerState(deadlines=tuple(deadlines), inventory=tuple(inventory))


def _view(row: PersonalLedgerEntry) -> LedgerEntryView:
    return LedgerEntryView(
        id=row.id,
        seq=int(row.seq),
        owner_user_id=row.owner_user_id,
        ledger=row.ledger,
        entry_type=row.entry_type,
        subject_key=row.subject_key,
        actor_kind=row.actor_kind,
        supersedes_entry_id=row.supersedes_entry_id,
        refers_to_entry_id=row.refers_to_entry_id,
        idempotency_key=row.idempotency_key,
        payload=dict(row.payload or {}),
        recorded_at=row.recorded_at,
    )


def _same_request(view: LedgerEntryView, request: Mapping[str, Any]) -> bool:
    return (
        view.ledger == request["ledger"]
        and view.entry_type == request["entry_type"]
        and view.subject_key == request["subject_key"]
        and view.actor_kind == request["actor_kind"]
        and view.supersedes_entry_id == request["supersedes_entry_id"]
        and view.refers_to_entry_id == request["refers_to_entry_id"]
        and dict(view.payload) == request["payload"]
    )


SessionFactory = Callable[[], Any]


class PersonalLedgerService:
    """利用者ごとに閉じた台帳の追記・参照・導出。"""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def _open(self):
        session = self._session_factory()
        if inspect.isawaitable(session):
            session = await session
        return session

    async def append_entry(
        self,
        *,
        actor: LedgerActor,
        ledger: str,
        entry_type: str,
        subject_key: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        supersedes_entry_id: Any = None,
        refers_to_entry_id: Any = None,
    ) -> AppendResult:
        """Own one transaction; materializers use the private shared-session core."""
        fields = dict(actor=actor, ledger=ledger, entry_type=entry_type, subject_key=subject_key,
                      payload=payload, idempotency_key=idempotency_key,
                      supersedes_entry_id=supersedes_entry_id, refers_to_entry_id=refers_to_entry_id)
        # Validate before opening a session, including the trusted actor boundary.
        self._validate_append(**fields)
        session = await self._open()
        try:
            async with session.begin():
                return await self._append_entry_in_transaction(session=session, **fields)
        finally:
            await session.close()

    def _validate_append(
        self,
        *,
        actor: LedgerActor,
        ledger: str,
        entry_type: str,
        subject_key: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        supersedes_entry_id: Any = None,
        refers_to_entry_id: Any = None,
    ) -> tuple[dict[str, Any], str]:
        if not isinstance(actor, LedgerActor) or actor.kind not in ("agent", "user_action"):
            raise LedgerPermissionError("invalid actor")
        if ledger not in PERSONAL_LEDGER_KINDS or entry_type not in PERSONAL_LEDGER_ENTRY_TYPES:
            raise LedgerValidationError("unsupported ledger or entry_type")
        expected_kind = _kind_of(entry_type)
        if expected_kind is not None and expected_kind != ledger:
            raise LedgerValidationError("entry_type does not belong to this ledger")
        if not isinstance(subject_key, str) or not _SLUG_RE.fullmatch(subject_key):
            raise LedgerValidationError("invalid subject_key")
        if not isinstance(idempotency_key, str) or not _KEY_RE.fullmatch(idempotency_key):
            raise LedgerValidationError("invalid idempotency_key")
        supersedes = _optional_uuid(supersedes_entry_id, "supersedes_entry_id")
        refers_to = _optional_uuid(refers_to_entry_id, "refers_to_entry_id")
        if entry_type in PERSONAL_LEDGER_USER_ONLY_ENTRY_TYPES:
            if actor.kind != "user_action":
                raise LedgerPermissionError("requires a trusted user action")
            if refers_to is None:
                raise LedgerValidationError("refers_to_entry_id is required")
        elif refers_to is not None:
            raise LedgerValidationError("refers_to_entry_id is not allowed for this entry_type")
        if entry_type == "void" and supersedes is None:
            raise LedgerValidationError("void requires supersedes_entry_id")
        normalized = normalize_payload(entry_type, payload)
        request = {
            "ledger": ledger,
            "entry_type": entry_type,
            "subject_key": subject_key,
            "actor_kind": actor.kind,
            "supersedes_entry_id": supersedes,
            "refers_to_entry_id": refers_to,
            "payload": normalized,
        }

        return request, idempotency_key

    async def _append_entry_in_transaction(self, *, session, **fields) -> AppendResult:
        """Private composition hook. Caller owns commit/rollback for ALL writes."""
        if not session.in_transaction():
            raise LedgerValidationError("an active transaction is required")
        request, idempotency_key = self._validate_append(**fields)
        actor = fields["actor"]
        ledger, entry_type = request["ledger"], request["entry_type"]
        subject_key, normalized = request["subject_key"], request["payload"]
        supersedes, refers_to = request["supersedes_entry_id"], request["refers_to_entry_id"]
        await self._lock_subject(session, actor.user_id, ledger, subject_key)
        replay = await self._find_by_key(session, actor.user_id, idempotency_key)
        if replay is not None:
            return self._replay(replay, request)
        await self._validate_relations(session, actor, request)
        entry_id = uuid4()
        if entry_type == "deadline_verification":
            await self._check_digest(session, actor.user_id, refers_to, normalized)
        row = PersonalLedgerEntry(
            id=entry_id,
            owner_user_id=actor.user_id,
            ledger=ledger,
            entry_type=entry_type,
            subject_key=subject_key,
            actor_kind=actor.kind,
            supersedes_entry_id=supersedes,
            refers_to_entry_id=refers_to,
            idempotency_key=idempotency_key,
            recorded_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        row.payload = normalized
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError as exc:
            name = str(getattr(getattr(exc, "orig", None), "constraint_name", "") or exc.orig)
            if "uq_personal_ledger_idempotency" in name:
                winner = await self._find_by_key(session, actor.user_id, idempotency_key)
                if winner is not None:
                    return self._replay(winner, request)
            if "uq_personal_ledger_single_successor" in name:
                raise LedgerConflictError("entry was already corrected") from None
            raise LedgerConflictError("ledger constraint violated") from None
        return AppendResult(entry=_view(row), replayed=False)

    @staticmethod
    def _replay(existing: PersonalLedgerEntry, request: Mapping[str, Any]) -> AppendResult:
        view = _view(existing)
        if not _same_request(view, request):
            raise LedgerConflictError("idempotency key was used with different content")
        return AppendResult(entry=view, replayed=True)

    @staticmethod
    async def _lock_subject(session, owner: UUID, ledger: str, subject_key: str) -> None:
        # 同じ (owner, ledger, subject) の書込みを直列化し、検証と挿入の間の競合を消す。
        key = zlib.crc32(f"{owner}:{ledger}:{subject_key}".encode("utf-8"))
        key = key - 2**32 if key >= 2**31 else key
        await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})

    @staticmethod
    async def _find_by_key(session, owner: UUID, key: str) -> PersonalLedgerEntry | None:
        return (
            await session.execute(
                select(PersonalLedgerEntry).where(
                    PersonalLedgerEntry.owner_user_id == owner,
                    PersonalLedgerEntry.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _get_owned(session, owner: UUID, entry_id: UUID) -> PersonalLedgerEntry:
        row = (
            await session.execute(
                select(PersonalLedgerEntry).where(
                    PersonalLedgerEntry.owner_user_id == owner,
                    PersonalLedgerEntry.id == entry_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise LedgerNotFoundError("entry not found")
        return row

    async def _is_superseded(self, session, owner: UUID, entry_id: UUID) -> bool:
        return (
            await session.execute(
                select(PersonalLedgerEntry.id).where(
                    PersonalLedgerEntry.owner_user_id == owner,
                    PersonalLedgerEntry.supersedes_entry_id == entry_id,
                )
            )
        ).first() is not None

    async def _validate_relations(self, session, actor: LedgerActor, request: Mapping[str, Any]) -> None:
        owner = actor.user_id
        entry_type = request["entry_type"]
        ledger, subject = request["ledger"], request["subject_key"]
        supersedes, refers_to = request["supersedes_entry_id"], request["refers_to_entry_id"]

        if supersedes is not None:
            target = await self._get_owned(session, owner, supersedes)
            if target.ledger != ledger or target.subject_key != subject:
                raise LedgerValidationError("supersedes target belongs to a different subject")
            if target.entry_type == "void":
                raise LedgerConflictError("a void entry cannot be corrected")
            if entry_type != "void" and entry_type != target.entry_type:
                raise LedgerValidationError("a correction must keep the entry_type")
            if target.entry_type in PERSONAL_LEDGER_USER_ONLY_ENTRY_TYPES and actor.kind != "user_action":
                raise LedgerPermissionError("requires a trusted user action")
            if await self._is_superseded(session, owner, supersedes):
                raise LedgerConflictError("entry was already corrected")
            if (
                entry_type in PERSONAL_LEDGER_USER_ONLY_ENTRY_TYPES
                and target.refers_to_entry_id != refers_to
            ):
                raise LedgerValidationError("a correction must refer to the same candidate")
        if refers_to is not None:
            target = await self._get_owned(session, owner, refers_to)
            if target.entry_type != "deadline_candidate" or target.subject_key != subject:
                raise LedgerValidationError("refers_to must be a candidate of the same subject")
            if await self._is_superseded(session, owner, refers_to):
                raise LedgerConflictError("candidate is no longer current")
            duplicate = (
                await session.execute(
                    select(PersonalLedgerEntry.id).where(
                        PersonalLedgerEntry.owner_user_id == owner,
                        PersonalLedgerEntry.entry_type == entry_type,
                        PersonalLedgerEntry.refers_to_entry_id == refers_to,
                    )
                )
            ).all()
            for (existing_id,) in duplicate:
                # 訂正対象そのもの（この挿入で後継を得る直前の行）は重複とみなさない。
                # 後継の一意性は supersedes の検査と部分ユニーク索引が守る。
                if existing_id == supersedes:
                    continue
                if not await self._is_superseded(session, owner, existing_id):
                    raise LedgerConflictError("already recorded for this candidate")
        if entry_type == "deadline_candidate" and supersedes is None:
            current = (
                await session.execute(
                    select(PersonalLedgerEntry.id).where(
                        PersonalLedgerEntry.owner_user_id == owner,
                        PersonalLedgerEntry.ledger == "deadline",
                        PersonalLedgerEntry.subject_key == subject,
                        PersonalLedgerEntry.entry_type == "deadline_candidate",
                    )
                )
            ).all()
            for (existing_id,) in current:
                if not await self._is_superseded(session, owner, existing_id):
                    raise LedgerConflictError("a current candidate exists; correct it instead")

    async def _check_digest(self, session, owner: UUID, candidate_id: UUID, payload: Mapping[str, Any]) -> None:
        cand = await self._get_owned(session, owner, candidate_id)
        expected = candidate_digest(
            entry_id=cand.id, subject_key=cand.subject_key, due_date=str(dict(cand.payload)["due_date"])
        )
        if payload["confirmed_digest"] != expected:
            raise LedgerConflictError("confirmed content does not match the current candidate")

    @staticmethod
    async def _select_entries(
        session, owner: UUID, *, ledger: str | None, subject_key: str | None, limit: int,
        guard: Callable[[], None] | None = None,
    ) -> list[LedgerEntryView]:
        stmt = select(PersonalLedgerEntry).where(PersonalLedgerEntry.owner_user_id == owner)
        if ledger is not None:
            stmt = stmt.where(PersonalLedgerEntry.ledger == ledger)
        if subject_key is not None:
            stmt = stmt.where(PersonalLedgerEntry.subject_key == subject_key)
        rows = (await session.execute(stmt.order_by(PersonalLedgerEntry.seq).limit(limit))).scalars().all()
        if guard is not None:
            guard()  # right after the DB await, before payloads are read/decrypted
        return [_view(row) for row in rows]

    async def list_entries(
        self,
        *,
        actor: LedgerActor,
        ledger: str | None = None,
        subject_key: str | None = None,
        limit: int = 500,
        guard: Callable[[], None] | None = None,
    ) -> list[LedgerEntryView]:
        """行を seq 昇順に返す。該当行が ``limit`` を超える場合は切り詰めずエラーにする。

        履歴の閲覧には ``list_entries_page`` を使う。
        """

        if not isinstance(actor, LedgerActor):
            raise LedgerPermissionError("invalid actor")
        if ledger is not None and ledger not in PERSONAL_LEDGER_KINDS:
            raise LedgerValidationError("unsupported ledger")
        safe_limit = min(max(int(limit), 1), MAX_STATE_ENTRIES)
        session = await self._open()
        try:
            if guard is not None:
                guard()  # after _open, before the DB read
            views = await self._select_entries(
                session, actor.user_id, ledger=ledger, subject_key=subject_key, limit=safe_limit + 1,
                guard=guard,
            )
        finally:
            await session.close()
        if len(views) > safe_limit:
            raise LedgerValidationError("too many entries to list at once; use list_entries_page")
        return views

    async def list_entries_page(
        self,
        *,
        actor: LedgerActor,
        ledger: str | None = None,
        subject_key: str | None = None,
        limit: int = 50,
        before_seq: int | None = None,
    ) -> LedgerPage:
        """新しい順の keyset ページ。復号するのはこのページの行だけ。

        superseded は SQL の EXISTS で全履歴（同じ owner のみ）に対して判定するので、
        後継がページの外にあっても正しい。順序は seq（一意・単調）なので重複も欠落も起きない。
        """

        if not isinstance(actor, LedgerActor):
            raise LedgerPermissionError("invalid actor")
        if ledger is not None and ledger not in PERSONAL_LEDGER_KINDS:
            raise LedgerValidationError("unsupported ledger")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise LedgerValidationError("invalid page size")
        if before_seq is not None and (isinstance(before_seq, bool) or not isinstance(before_seq, int) or before_seq < 1):
            raise LedgerValidationError("invalid cursor")
        owner = actor.user_id
        successor = aliased(PersonalLedgerEntry)
        superseded = (
            select(successor.id)
            .where(successor.owner_user_id == owner, successor.supersedes_entry_id == PersonalLedgerEntry.id)
            .exists()
        )
        stmt = select(PersonalLedgerEntry, superseded.label("is_superseded")).where(
            PersonalLedgerEntry.owner_user_id == owner
        )
        if ledger is not None:
            stmt = stmt.where(PersonalLedgerEntry.ledger == ledger)
        if subject_key is not None:
            stmt = stmt.where(PersonalLedgerEntry.subject_key == subject_key)
        if before_seq is not None:
            stmt = stmt.where(PersonalLedgerEntry.seq < before_seq)
        session = await self._open()
        try:
            rows = (await session.execute(stmt.order_by(PersonalLedgerEntry.seq.desc()).limit(limit + 1))).all()
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            views = tuple(_view(row) for row, _ in page_rows)
            flagged = frozenset(row.id for row, is_superseded in page_rows if is_superseded)
        finally:
            await session.close()
        return LedgerPage(
            entries=views,
            superseded_ids=flagged,
            next_before_seq=views[-1].seq if has_more and views else None,
        )

    async def get_entry(self, *, actor: LedgerActor, entry_id: Any) -> LedgerEntryView:
        """自分の行を1件取得する。存在しない行と他人の行は区別しない。"""

        if not isinstance(actor, LedgerActor):
            raise LedgerPermissionError("invalid actor")
        session = await self._open()
        try:
            return _view(await self._get_owned(session, actor.user_id, _uuid(entry_id, "entry_id")))
        finally:
            await session.close()

    async def current_state(self, *, actor: LedgerActor) -> LedgerState:
        """全履歴から導出する。履歴が上限を超える場合は部分結果を返さずエラーにする。"""

        if not isinstance(actor, LedgerActor):
            raise LedgerPermissionError("invalid actor")
        session = await self._open()
        try:
            # 上限 + 1 件まで読み、超過を確実に検出する（list_entries の切り詰めとは別経路）。
            entries = await self._select_entries(
                session, actor.user_id, ledger=None, subject_key=None, limit=MAX_STATE_ENTRIES + 1
            )
        finally:
            await session.close()
        if len(entries) > MAX_STATE_ENTRIES:
            raise LedgerValidationError("ledger too large to derive completely; no partial state is returned")
        return derive_state(entries)
