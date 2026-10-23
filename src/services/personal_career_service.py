"""個人のキャリア情報（職務プロフィール・求人source）のサービス。

責務と境界:
- owner ごとに閉じたプロフィール版と求人sourceを、追記専用の履歴として扱う。
- プロフィール本文は暗号化して保存し、所有者本人の明示確認がある版だけを
  context adapter が出す。未確認のスキル・給与・求人クレームは決して authoritative
  context に混ぜない。
- 家計（finance）とはテーブル・FK・DTOを共有しない。推測、スクレイピング、
  ログイン、応募、連絡は行わない。
- ここは保存service。本人のsource/profile操作はowner HTTP routesから行い、実行時は個人Agent
  controllerが確認済みprofileを指定したread-only context adapterだけを呼ぶ。serverの
  model-tool registryやmodel初期化には接続しない。

limit と失敗時の方針:
- 壊れた保存payloadや digest 不一致は fail-closed（その版を権威として採用しない）。
- 同じ idempotency_key の再送は同じ結果を返し、内容が違えば Conflict。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError

from .personal_async_guard import run_personal_guard
from src.memory.models.personal_career import (
    MAX_CLAIM_CHARS,
    MAX_PROFILE_ITEMS,
    MAX_SOURCE_BYTES,
    MAX_SOURCE_REFS,
    MAX_UNCONFIRMED_CLAIMS,
    SKILL_MAX_CHARS,
    TEXT_MAX_CHARS,
    PersonalCareerProfileConfirmation,
    PersonalCareerProfileVersion,
    PersonalCareerSource,
    PersonalCareerSourceSelection,
)

PROFILE_FIELDS = ("skills", "preferences", "constraints")
CLAIM_KINDS = ("skill", "salary", "job", "other")
CLAIM_ORIGINS = ("model", "import")

USER_ACTION = "user_action"
MODEL_ACTION = "model"

SOURCE_DTO_KEYS = frozenset(
    ("source_ref", "source_sha256", "mime_type", "byte_size")
)
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MIME_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}/[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
)

# context adapter の固定上限（呼び出し側で緩められない）。
CONTEXT_HARD_MAX_ITEMS = 32
CONTEXT_HARD_MAX_TEXT_CHARS = 200
CONTEXT_HARD_MAX_SOURCES = 16
CONTEXT_HARD_MAX_TOTAL_CHARS = 4000
CONTEXT_SCAN_VERSIONS = 50

NEEDS_REVIEW_NO_CONFIRMED_PROFILE = "no_confirmed_profile"
NEEDS_REVIEW_UNCONFIRMED_CLAIMS = "unconfirmed_claims_excluded"
NEEDS_REVIEW_PROFILE_TRUNCATED = "profile_truncated"
NEEDS_REVIEW_SOURCES_TRUNCATED = "sources_truncated"
NEEDS_REVIEW_SOURCE_SELECTION_EMPTY = "no_selected_source_refs"
NEEDS_REVIEW_DIGEST_MISMATCH = "confirmation_digest_mismatch"
NEEDS_REVIEW_PAYLOAD_UNREADABLE = "confirmed_profile_unreadable"


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class CareerError(Exception):
    """キャリア情報サービスの基底例外。"""

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message or self.__class__.__name__


class CareerValidationError(CareerError):
    """入力DTO・境界の違反。"""


class CareerNotFoundError(CareerError):
    """owner の範囲内に存在しない。"""


class CareerConflictError(CareerError):
    """idempotency 不一致、または追記専用履歴との衝突。"""


class CareerPermissionError(CareerError):
    """owner 以外・model 主体による禁止操作。"""


# ---------------------------------------------------------------------------
# actor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CareerActor:
    """操作主体。owner_user_id は trusted caller が渡す認証済み user のみ。

    ``kind`` は ``user_action``（所有者本人の操作）か ``model``（提案のみ）に限定する。
    model 主体は authoritative フィールドを作れない。
    """

    user_id: uuid.UUID
    kind: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", as_uuid(self.user_id))
        if self.kind not in (USER_ACTION, MODEL_ACTION):
            raise ValueError("unsupported career actor kind")


def owner_actor(user_id: Any) -> CareerActor:
    """所有者本人の操作主体を作る（認証済みセッションからのみ）。"""

    return CareerActor(as_uuid(user_id), USER_ACTION)


def model_actor(user_id: Any) -> CareerActor:
    """モデル提案の主体。authoritative は書けず、未確認クレームのみ登録できる。"""

    return CareerActor(as_uuid(user_id), MODEL_ACTION)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CareerSourceView:
    """固定 source DTO。パス・URL・本文は含めない。"""

    source_ref: uuid.UUID
    source_sha256: str
    mime_type: str
    byte_size: int

    def to_contract(self) -> dict[str, Any]:
        return {
            "source_ref": str(self.source_ref),
            "source_sha256": self.source_sha256,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
        }


@dataclass(frozen=True)
class CareerSourceStateView:
    """source と現在の選択状態。"""

    source: CareerSourceView
    selected: bool


@dataclass(frozen=True)
class CareerProfileFields:
    skills: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()

    def to_contract(self) -> dict[str, list[str]]:
        return {
            "skills": list(self.skills),
            "preferences": list(self.preferences),
            "constraints": list(self.constraints),
        }


@dataclass(frozen=True)
class UnconfirmedClaim:
    """未確認クレーム。authoritative context には決して出さない。"""

    kind: str
    text: str
    origin: str


@dataclass(frozen=True)
class CareerProfileVersionView:
    version_ref: uuid.UUID
    version: int
    confirmed: bool
    confirmed_digest: str
    profile: CareerProfileFields
    source_refs: tuple[uuid.UUID, ...]
    unconfirmed_claim_count: int
    actor_kind: str
    recorded_at: datetime | None


@dataclass(frozen=True)
class CareerConfirmationView:
    version_ref: uuid.UUID
    version: int
    confirmed_digest: str
    status: str = "confirmed"

    def to_contract(self) -> dict[str, Any]:
        return {
            "version_ref": str(self.version_ref),
            "version": self.version,
            "confirmed_digest": self.confirmed_digest,
            "status": self.status,
        }


@dataclass(frozen=True)
class CareerContextBounds:
    """context の呼び出し上限。ハード上限を超える値は拒否する。"""

    max_items: int = 12
    max_text_chars: int = 160
    max_sources: int = 8
    max_total_chars: int = 2000

    def __post_init__(self) -> None:
        _require_bounded_int("max_items", self.max_items, 0, CONTEXT_HARD_MAX_ITEMS)
        _require_bounded_int(
            "max_text_chars", self.max_text_chars, 1, CONTEXT_HARD_MAX_TEXT_CHARS
        )
        _require_bounded_int("max_sources", self.max_sources, 0, CONTEXT_HARD_MAX_SOURCES)
        _require_bounded_int(
            "max_total_chars", self.max_total_chars, 1, CONTEXT_HARD_MAX_TOTAL_CHARS
        )


@dataclass(frozen=True)
class CareerContext:
    """``private.career.context`` の固定出力。確認済み内容と選択sourceだけ。"""

    profile: CareerProfileFields
    source_refs: tuple[uuid.UUID, ...]
    needs_review: tuple[str, ...]

    def to_contract(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_contract(),
            "source_refs": [str(ref) for ref in self.source_refs],
            "needs_review": list(self.needs_review),
        }


# ---------------------------------------------------------------------------
# pure validation / normalization
# ---------------------------------------------------------------------------


def as_uuid(value: Any) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise CareerValidationError("uuid_invalid") from exc
    raise CareerValidationError("uuid_invalid")


def _require_bounded_int(name: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CareerValidationError(f"{name}_invalid")
    if value < low or value > high:
        raise CareerValidationError(f"{name}_out_of_range")
    return value


def _normalize_text(value: str, max_chars: int, field_name: str) -> str:
    if not isinstance(value, str):
        raise CareerValidationError(f"{field_name}_invalid")
    for char in value:
        code = ord(char)
        if code < 0x20 and char not in "\n\r\t":
            raise CareerValidationError(f"{field_name}_control_char")
        if code == 0x7F:
            raise CareerValidationError(f"{field_name}_control_char")
    text = " ".join(value.split())
    if not text:
        raise CareerValidationError(f"{field_name}_empty")
    if len(text) > max_chars:
        raise CareerValidationError(f"{field_name}_too_long")
    return text


def normalize_profile(profile: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]]:
    """authoritative プロフィールを固定キー・件数・文字数に正規化する。

    未知キー（salary 等）は拡張ではなく拒否する。
    """

    if profile is None:
        profile = {}
    if not isinstance(profile, Mapping):
        raise CareerValidationError("profile_invalid")
    unknown = set(profile) - set(PROFILE_FIELDS)
    if unknown:
        raise CareerValidationError("profile_unknown_field")

    normalized: dict[str, tuple[str, ...]] = {}
    for field_name in PROFILE_FIELDS:
        limit = SKILL_MAX_CHARS if field_name == "skills" else TEXT_MAX_CHARS
        normalized[field_name] = _normalize_items(
            profile.get(field_name, ()), MAX_PROFILE_ITEMS, limit, field_name
        )
    return normalized


def _normalize_items(
    values: Any, max_items: int, max_chars: int, field_name: str
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise CareerValidationError(f"{field_name}_invalid")
    items: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = _normalize_text(raw, max_chars, field_name)
        if text in seen:
            continue
        seen.add(text)
        items.append(text)
        if len(items) > max_items:
            raise CareerValidationError(f"{field_name}_too_many")
    return tuple(items)


def normalize_claims(claims: Any) -> tuple[dict[str, str], ...]:
    """未確認クレームを正規化する。status を自称させない（常に未確認）。"""

    if claims is None:
        return ()
    if isinstance(claims, (str, bytes)) or not isinstance(claims, Iterable):
        raise CareerValidationError("claims_invalid")
    normalized: list[dict[str, str]] = []
    for raw in claims:
        if not isinstance(raw, Mapping):
            raise CareerValidationError("claim_invalid")
        if set(raw) != {"kind", "text", "origin"}:
            raise CareerValidationError("claim_unknown_field")
        kind = raw["kind"]
        origin = raw["origin"]
        if kind not in CLAIM_KINDS:
            raise CareerValidationError("claim_kind_invalid")
        if origin not in CLAIM_ORIGINS:
            raise CareerValidationError("claim_origin_invalid")
        text = _normalize_text(raw["text"], MAX_CLAIM_CHARS, "claim_text")
        normalized.append({"kind": kind, "text": text, "origin": origin})
        if len(normalized) > MAX_UNCONFIRMED_CLAIMS:
            raise CareerValidationError("claims_too_many")
    return tuple(normalized)


def normalize_source_dto(source: Mapping[str, Any]) -> CareerSourceView:
    """固定 source DTO を厳密に検証する。余分なパス/URLキーは拒否。"""

    if not isinstance(source, Mapping):
        raise CareerValidationError("source_invalid")
    if set(source) != SOURCE_DTO_KEYS:
        raise CareerValidationError("source_unknown_field")
    source_ref = as_uuid(source["source_ref"])
    sha = source["source_sha256"]
    if not isinstance(sha, str) or not SHA256_PATTERN.fullmatch(sha):
        raise CareerValidationError("source_sha256_invalid")
    mime = source["mime_type"]
    if not isinstance(mime, str) or not MIME_PATTERN.fullmatch(mime):
        raise CareerValidationError("source_mime_type_invalid")
    byte_size = source["byte_size"]
    if isinstance(byte_size, bool) or not isinstance(byte_size, int):
        raise CareerValidationError("source_byte_size_invalid")
    if byte_size < 1 or byte_size > MAX_SOURCE_BYTES:
        raise CareerValidationError("source_byte_size_out_of_range")
    return CareerSourceView(source_ref, sha, mime, byte_size)


def validate_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or not IDEMPOTENCY_PATTERN.fullmatch(value):
        raise CareerValidationError("idempotency_key_invalid")
    return value


def normalize_source_refs(source_refs: Any) -> tuple[uuid.UUID, ...]:
    if source_refs is None:
        return ()
    if isinstance(source_refs, (str, bytes)) or not isinstance(source_refs, Iterable):
        raise CareerValidationError("source_refs_invalid")
    refs: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for raw in source_refs:
        ref = as_uuid(raw)
        if ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
        if len(refs) > MAX_SOURCE_REFS:
            raise CareerValidationError("source_refs_too_many")
    return tuple(refs)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def profile_digest(
    version: int,
    profile: Mapping[str, Sequence[str]],
    source_refs: Sequence[Any],
) -> str:
    """確認対象digest。authoritative と source 参照だけを含め、未確認クレームは含めない。"""

    body = {
        "version": version,
        "profile": {name: list(profile.get(name, ())) for name in PROFILE_FIELDS},
        "source_refs": sorted({str(as_uuid(ref)) for ref in source_refs}),
    }
    return hashlib.sha256(canonical_json(body).encode("ascii")).hexdigest()


# ---------------------------------------------------------------------------
# context assembly (pure)
# ---------------------------------------------------------------------------


def assemble_context(
    profile: Mapping[str, Sequence[str]],
    source_refs: Sequence[uuid.UUID],
    *,
    has_unconfirmed_claims: bool,
    bounds: CareerContextBounds,
) -> CareerContext:
    """確認済み profile と選択済み source から、上限付き context を組み立てる。

    渡される profile は呼び出し側で「確認済み・正規化済み」であることが前提。
    未確認クレームの本文はここに存在しない。
    """

    needs_review: list[str] = []
    profile_truncated = False
    sources_truncated = False
    used = 0
    bounded_profile: dict[str, list[str]] = {}

    for field_name in PROFILE_FIELDS:
        values = tuple(profile.get(field_name, ()))
        field_values: list[str] = []
        for item in values:
            if len(field_values) >= bounds.max_items:
                profile_truncated = True
                break
            text = item[: bounds.max_text_chars]
            if text != item:
                profile_truncated = True
            if not text:
                continue
            if used + len(text) > bounds.max_total_chars:
                profile_truncated = True
                break
            used += len(text)
            field_values.append(text)
        bounded_profile[field_name] = field_values

    refs = tuple(source_refs)
    if len(refs) > bounds.max_sources:
        refs = refs[: bounds.max_sources]
        sources_truncated = True

    if has_unconfirmed_claims:
        needs_review.append(NEEDS_REVIEW_UNCONFIRMED_CLAIMS)
    if profile_truncated:
        needs_review.append(NEEDS_REVIEW_PROFILE_TRUNCATED)
    if sources_truncated:
        needs_review.append(NEEDS_REVIEW_SOURCES_TRUNCATED)
    if not refs:
        needs_review.append(NEEDS_REVIEW_SOURCE_SELECTION_EMPTY)

    return CareerContext(
        profile=CareerProfileFields(
            skills=tuple(bounded_profile["skills"]),
            preferences=tuple(bounded_profile["preferences"]),
            constraints=tuple(bounded_profile["constraints"]),
        ),
        source_refs=refs,
        needs_review=tuple(needs_review),
    )


def _empty_context(*extra: str) -> CareerContext:
    return CareerContext(
        profile=CareerProfileFields(),
        source_refs=(),
        needs_review=tuple(dict.fromkeys(extra)),
    )


# ---------------------------------------------------------------------------
# persistence port
# ---------------------------------------------------------------------------


class CareerRepository(Protocol):
    """owner で必ず絞る永続化ポート。SQL実装とテスト用実装が同じ契約を満たす。"""

    async def get_source(
        self, owner_user_id: uuid.UUID, source_ref: uuid.UUID
    ) -> PersonalCareerSource | None: ...

    async def get_source_by_idempotency(
        self, owner_user_id: uuid.UUID, idempotency_key: str
    ) -> PersonalCareerSource | None: ...

    async def add_source(
        self, row: PersonalCareerSource
    ) -> PersonalCareerSource: ...

    async def get_latest_selection(
        self, owner_user_id: uuid.UUID, source_id: uuid.UUID
    ) -> PersonalCareerSourceSelection | None: ...

    async def get_selection_by_idempotency(
        self, owner_user_id: uuid.UUID, idempotency_key: str
    ) -> PersonalCareerSourceSelection | None: ...

    async def add_selection(
        self, row: PersonalCareerSourceSelection
    ) -> PersonalCareerSourceSelection: ...

    async def list_source_states(
        self, owner_user_id: uuid.UUID, limit: int
    ) -> Sequence[tuple[PersonalCareerSource, bool]]: ...

    async def list_selected_sources(
        self, owner_user_id: uuid.UUID, limit: int
    ) -> Sequence[PersonalCareerSource]: ...

    async def get_version(
        self, owner_user_id: uuid.UUID, version_id: uuid.UUID
    ) -> PersonalCareerProfileVersion | None: ...

    async def get_version_by_idempotency(
        self, owner_user_id: uuid.UUID, idempotency_key: str
    ) -> PersonalCareerProfileVersion | None: ...

    async def max_version(self, owner_user_id: uuid.UUID) -> int: ...

    async def add_version(
        self, row: PersonalCareerProfileVersion
    ) -> PersonalCareerProfileVersion: ...

    async def get_confirmation(
        self, owner_user_id: uuid.UUID, version_id: uuid.UUID
    ) -> PersonalCareerProfileConfirmation | None: ...

    async def get_confirmation_by_idempotency(
        self, owner_user_id: uuid.UUID, idempotency_key: str
    ) -> PersonalCareerProfileConfirmation | None: ...

    async def add_confirmation(
        self, row: PersonalCareerProfileConfirmation
    ) -> PersonalCareerProfileConfirmation: ...

    async def list_versions(
        self, owner_user_id: uuid.UUID, limit: int
    ) -> Sequence[PersonalCareerProfileVersion]: ...

    async def list_versions_with_confirmation(
        self, owner_user_id: uuid.UUID, limit: int
    ) -> Sequence[
        tuple[PersonalCareerProfileVersion, PersonalCareerProfileConfirmation | None]
    ]: ...


class SqlCareerRepository:
    """SQLAlchemy async session による実装。"""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def _add(self, row: Any) -> Any:
        async with self._session_factory() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise CareerConflictError("append_only_conflict") from exc
            await session.refresh(row)
            return row

    async def get_source(
        self, owner_user_id: uuid.UUID, source_ref: uuid.UUID
    ) -> PersonalCareerSource | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    select(PersonalCareerSource).where(
                        PersonalCareerSource.owner_user_id == owner_user_id,
                        PersonalCareerSource.source_ref == source_ref,
                    )
                )
            ).scalar_one_or_none()

    async def get_source_by_idempotency(
        self, owner_user_id: uuid.UUID, idempotency_key: str
    ) -> PersonalCareerSource | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    select(PersonalCareerSource).where(
                        PersonalCareerSource.owner_user_id == owner_user_id,
                        PersonalCareerSource.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()

    async def add_source(self, row: PersonalCareerSource) -> PersonalCareerSource:
        return await self._add(row)

    async def get_latest_selection(
        self, owner_user_id: uuid.UUID, source_id: uuid.UUID
    ) -> PersonalCareerSourceSelection | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    select(PersonalCareerSourceSelection)
                    .where(
                        PersonalCareerSourceSelection.owner_user_id == owner_user_id,
                        PersonalCareerSourceSelection.source_id == source_id,
                    )
                    .order_by(PersonalCareerSourceSelection.seq.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def get_selection_by_idempotency(
        self, owner_user_id: uuid.UUID, idempotency_key: str
    ) -> PersonalCareerSourceSelection | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    select(PersonalCareerSourceSelection).where(
                        PersonalCareerSourceSelection.owner_user_id == owner_user_id,
                        PersonalCareerSourceSelection.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()

    async def add_selection(
        self, row: PersonalCareerSourceSelection
    ) -> PersonalCareerSourceSelection:
        return await self._add(row)

    async def list_source_states(
        self, owner_user_id: uuid.UUID, limit: int
    ) -> Sequence[tuple[PersonalCareerSource, bool]]:
        latest = (
            select(
                PersonalCareerSourceSelection.source_id.label("source_id"),
                func.max(PersonalCareerSourceSelection.seq).label("max_seq"),
            )
            .where(PersonalCareerSourceSelection.owner_user_id == owner_user_id)
            .group_by(PersonalCareerSourceSelection.source_id)
            .subquery()
        )
        statement = (
            select(PersonalCareerSource, PersonalCareerSourceSelection.selected)
            .outerjoin(
                latest, latest.c.source_id == PersonalCareerSource.id
            )
            .outerjoin(
                PersonalCareerSourceSelection,
                and_(
                    PersonalCareerSourceSelection.source_id == PersonalCareerSource.id,
                    PersonalCareerSourceSelection.seq == latest.c.max_seq,
                ),
            )
            .where(PersonalCareerSource.owner_user_id == owner_user_id)
            .order_by(PersonalCareerSource.seq.asc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return tuple((row[0], bool(row[1])) for row in rows)

    async def list_selected_sources(
        self, owner_user_id: uuid.UUID, limit: int
    ) -> Sequence[PersonalCareerSource]:
        latest = (
            select(
                PersonalCareerSourceSelection.source_id.label("source_id"),
                func.max(PersonalCareerSourceSelection.seq).label("max_seq"),
            )
            .where(PersonalCareerSourceSelection.owner_user_id == owner_user_id)
            .group_by(PersonalCareerSourceSelection.source_id)
            .subquery()
        )
        statement = (
            select(PersonalCareerSource)
            .join(latest, latest.c.source_id == PersonalCareerSource.id)
            .join(
                PersonalCareerSourceSelection,
                and_(
                    PersonalCareerSourceSelection.source_id == PersonalCareerSource.id,
                    PersonalCareerSourceSelection.seq == latest.c.max_seq,
                ),
            )
            .where(
                PersonalCareerSource.owner_user_id == owner_user_id,
                PersonalCareerSourceSelection.selected.is_(True),
            )
            .order_by(PersonalCareerSource.seq.asc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).scalars().all()
        return tuple(rows)

    async def get_version(
        self, owner_user_id: uuid.UUID, version_id: uuid.UUID
    ) -> PersonalCareerProfileVersion | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    select(PersonalCareerProfileVersion).where(
                        PersonalCareerProfileVersion.owner_user_id == owner_user_id,
                        PersonalCareerProfileVersion.id == version_id,
                    )
                )
            ).scalar_one_or_none()

    async def get_version_by_idempotency(
        self, owner_user_id: uuid.UUID, idempotency_key: str
    ) -> PersonalCareerProfileVersion | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    select(PersonalCareerProfileVersion).where(
                        PersonalCareerProfileVersion.owner_user_id == owner_user_id,
                        PersonalCareerProfileVersion.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()

    async def max_version(self, owner_user_id: uuid.UUID) -> int:
        async with self._session_factory() as session:
            value = (
                await session.execute(
                    select(func.max(PersonalCareerProfileVersion.version)).where(
                        PersonalCareerProfileVersion.owner_user_id == owner_user_id
                    )
                )
            ).scalar_one_or_none()
        return int(value or 0)

    async def add_version(
        self, row: PersonalCareerProfileVersion
    ) -> PersonalCareerProfileVersion:
        return await self._add(row)

    async def get_confirmation(
        self, owner_user_id: uuid.UUID, version_id: uuid.UUID
    ) -> PersonalCareerProfileConfirmation | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    select(PersonalCareerProfileConfirmation).where(
                        PersonalCareerProfileConfirmation.owner_user_id
                        == owner_user_id,
                        PersonalCareerProfileConfirmation.version_id == version_id,
                    )
                )
            ).scalar_one_or_none()

    async def get_confirmation_by_idempotency(
        self, owner_user_id: uuid.UUID, idempotency_key: str
    ) -> PersonalCareerProfileConfirmation | None:
        async with self._session_factory() as session:
            return (
                await session.execute(
                    select(PersonalCareerProfileConfirmation).where(
                        PersonalCareerProfileConfirmation.owner_user_id
                        == owner_user_id,
                        PersonalCareerProfileConfirmation.idempotency_key
                        == idempotency_key,
                    )
                )
            ).scalar_one_or_none()

    async def add_confirmation(
        self, row: PersonalCareerProfileConfirmation
    ) -> PersonalCareerProfileConfirmation:
        return await self._add(row)

    async def list_versions(
        self, owner_user_id: uuid.UUID, limit: int
    ) -> Sequence[PersonalCareerProfileVersion]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(PersonalCareerProfileVersion)
                    .where(PersonalCareerProfileVersion.owner_user_id == owner_user_id)
                    .order_by(PersonalCareerProfileVersion.version.desc())
                    .limit(limit)
                )
            ).scalars().all()
        return tuple(rows)

    async def list_versions_with_confirmation(
        self, owner_user_id: uuid.UUID, limit: int
    ) -> Sequence[
        tuple[PersonalCareerProfileVersion, PersonalCareerProfileConfirmation | None]
    ]:
        statement = (
            select(PersonalCareerProfileVersion, PersonalCareerProfileConfirmation)
            .outerjoin(
                PersonalCareerProfileConfirmation,
                and_(
                    PersonalCareerProfileConfirmation.owner_user_id
                    == PersonalCareerProfileVersion.owner_user_id,
                    PersonalCareerProfileConfirmation.version_id
                    == PersonalCareerProfileVersion.id,
                ),
            )
            .where(PersonalCareerProfileVersion.owner_user_id == owner_user_id)
            .order_by(PersonalCareerProfileVersion.version.desc())
            .limit(limit)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return tuple((row[0], row[1]) for row in rows)


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class PersonalCareerService:
    """Owner-scoped career history and explicit accepted-profile context service."""

    def __init__(self, repository: CareerRepository) -> None:
        self._repository = repository

    @classmethod
    def from_session_factory(cls, session_factory: Any) -> "PersonalCareerService":
        return cls(SqlCareerRepository(session_factory))

    # --- payload helpers ---------------------------------------------------

    @staticmethod
    def _read_payload(row: PersonalCareerProfileVersion) -> dict[str, Any]:
        payload = row.payload
        if not isinstance(payload, Mapping):
            raise CareerValidationError("stored_payload_invalid")
        if set(payload) != {"profile", "source_refs", "unconfirmed"}:
            raise CareerValidationError("stored_payload_invalid")
        profile = normalize_profile(payload["profile"])
        try:
            refs = normalize_source_refs(payload["source_refs"])
        except CareerValidationError as exc:
            raise CareerValidationError("stored_payload_invalid") from exc
        claims = normalize_claims(payload["unconfirmed"])
        return {"profile": profile, "source_refs": refs, "unconfirmed": claims}

    @staticmethod
    def _source_view(row: PersonalCareerSource) -> CareerSourceView:
        return CareerSourceView(
            source_ref=row.source_ref,
            source_sha256=row.source_sha256,
            mime_type=row.mime_type,
            byte_size=row.byte_size,
        )

    def _version_view(
        self,
        row: PersonalCareerProfileVersion,
        confirmation: PersonalCareerProfileConfirmation | None,
    ) -> CareerProfileVersionView:
        payload = self._read_payload(row)
        digest = profile_digest(
            row.version, payload["profile"], payload["source_refs"]
        )
        confirmed = (
            confirmation is not None
            and confirmation.confirmed_digest == digest
            and confirmation.owner_user_id == row.owner_user_id
        )
        return CareerProfileVersionView(
            version_ref=row.id,
            version=row.version,
            confirmed=confirmed,
            confirmed_digest=digest,
            profile=CareerProfileFields(
                skills=payload["profile"]["skills"],
                preferences=payload["profile"]["preferences"],
                constraints=payload["profile"]["constraints"],
            ),
            source_refs=payload["source_refs"],
            unconfirmed_claim_count=len(payload["unconfirmed"]),
            actor_kind=row.actor_kind,
            recorded_at=row.recorded_at,
        )

    # --- source history ----------------------------------------------------

    async def register_source(
        self,
        actor: CareerActor,
        source: Mapping[str, Any],
        idempotency_key: str,
    ) -> CareerSourceView:
        """owner が選択対象としてsourceを登録する。本文は保存しない。"""

        self._require_user_action(actor)
        key = validate_idempotency_key(idempotency_key)
        dto = normalize_source_dto(source)
        existing = await self._repository.get_source_by_idempotency(
            actor.user_id, key
        )
        if existing is not None:
            if (
                existing.source_ref != dto.source_ref
                or existing.source_sha256 != dto.source_sha256
                or existing.mime_type != dto.mime_type
                or existing.byte_size != dto.byte_size
            ):
                raise CareerConflictError("idempotency_payload_mismatch")
            return self._source_view(existing)

        duplicate = await self._repository.get_source(
            actor.user_id, dto.source_ref
        )
        if duplicate is not None:
            raise CareerConflictError("source_ref_already_registered")

        row = PersonalCareerSource(
            owner_user_id=actor.user_id,
            source_ref=dto.source_ref,
            source_sha256=dto.source_sha256,
            mime_type=dto.mime_type,
            byte_size=dto.byte_size,
            actor_kind=actor.kind,
            idempotency_key=key,
        )
        return self._source_view(await self._repository.add_source(row))

    async def set_source_selected(
        self,
        actor: CareerActor,
        source_ref: Any,
        selected: bool,
        idempotency_key: str,
    ) -> CareerSourceStateView:
        """owner 本人による選択/解除。履歴は追記のみ。"""

        self._require_user_action(actor)
        if not isinstance(selected, bool):
            raise CareerValidationError("selected_invalid")
        key = validate_idempotency_key(idempotency_key)
        ref = as_uuid(source_ref)
        source = await self._repository.get_source(actor.user_id, ref)
        if source is None:
            raise CareerNotFoundError("source_not_found")
        if source.owner_user_id != actor.user_id:
            raise CareerNotFoundError("source_not_found")

        existing = await self._repository.get_selection_by_idempotency(
            actor.user_id, key
        )
        if existing is not None:
            if existing.source_id != source.id or existing.selected is not selected:
                raise CareerConflictError("idempotency_payload_mismatch")
            return CareerSourceStateView(self._source_view(source), selected)

        row = PersonalCareerSourceSelection(
            owner_user_id=actor.user_id,
            source_id=source.id,
            selected=selected,
            actor_kind=actor.kind,
            idempotency_key=key,
        )
        await self._repository.add_selection(row)
        return CareerSourceStateView(self._source_view(source), selected)

    async def list_sources(
        self, actor: CareerActor, limit: int = 50
    ) -> tuple[CareerSourceStateView, ...]:
        limit = _require_bounded_int("limit", limit, 1, 200)
        rows = await self._repository.list_source_states(actor.user_id, limit)
        return tuple(
            CareerSourceStateView(self._source_view(source), bool(selected))
            for source, selected in rows
        )

    async def list_selected_sources(
        self, actor: CareerActor, limit: int = 16
    ) -> tuple[CareerSourceView, ...]:
        limit = _require_bounded_int("limit", limit, 1, 64)
        rows = await self._repository.list_selected_sources(actor.user_id, limit)
        return tuple(self._source_view(row) for row in rows)

    # --- profile versions --------------------------------------------------

    async def create_profile_version(
        self,
        actor: CareerActor,
        *,
        profile: Mapping[str, Any] | None,
        source_refs: Sequence[Any],
        unconfirmed_claims: Sequence[Mapping[str, Any]] | None,
        idempotency_key: str,
    ) -> CareerProfileVersionView:
        """新しいプロフィール版を追記する。owner 本人以外は authoritative を作れない。"""

        key = validate_idempotency_key(idempotency_key)
        normalized = normalize_profile(profile)
        claims = normalize_claims(unconfirmed_claims)
        refs = normalize_source_refs(source_refs)

        if actor.kind == MODEL_ACTION:
            if any(normalized[name] for name in PROFILE_FIELDS):
                raise CareerPermissionError("model_cannot_author_profile")
            if refs:
                raise CareerPermissionError("model_cannot_select_sources")
            if not claims:
                raise CareerValidationError("model_claim_required")
        else:
            self._require_user_action(actor)

        for ref in refs:
            source = await self._repository.get_source(actor.user_id, ref)
            if source is None or source.owner_user_id != actor.user_id:
                raise CareerValidationError("source_not_registered")
            selection = await self._repository.get_latest_selection(
                actor.user_id, source.id
            )
            if selection is None or not selection.selected:
                raise CareerValidationError("source_not_selected")

        payload = {
            "profile": {name: list(normalized[name]) for name in PROFILE_FIELDS},
            "source_refs": [str(ref) for ref in refs],
            "unconfirmed": [dict(claim) for claim in claims],
        }

        existing = await self._repository.get_version_by_idempotency(
            actor.user_id, key
        )
        if existing is not None:
            stored = self._read_payload(existing)
            if canonical_json(
                {
                    "profile": {
                        name: list(stored["profile"][name]) for name in PROFILE_FIELDS
                    },
                    "source_refs": [str(ref) for ref in stored["source_refs"]],
                    "unconfirmed": [dict(claim) for claim in stored["unconfirmed"]],
                }
            ) != canonical_json(payload):
                raise CareerConflictError("idempotency_payload_mismatch")
            replay_confirmation = await self._repository.get_confirmation(
                actor.user_id, existing.id
            )
            return self._version_view(existing, replay_confirmation)

        next_version = await self._repository.max_version(actor.user_id) + 1
        row = PersonalCareerProfileVersion(
            owner_user_id=actor.user_id,
            version=next_version,
            actor_kind=actor.kind,
            payload=payload,
            idempotency_key=key,
        )
        stored_row = await self._repository.add_version(row)
        return self._version_view(stored_row, None)

    async def confirm_profile_version(
        self,
        actor: CareerActor,
        *,
        version_ref: Any,
        confirmed_digest: str,
        idempotency_key: str,
        owner_confirmed: bool,
    ) -> CareerConfirmationView:
        """所有者本人の明示確認。digest が一致しない確認は拒否する。"""

        self._require_user_action(actor)
        if owner_confirmed is not True:
            raise CareerPermissionError("owner_confirmation_required")
        key = validate_idempotency_key(idempotency_key)
        if not isinstance(confirmed_digest, str) or not SHA256_PATTERN.fullmatch(
            confirmed_digest
        ):
            raise CareerValidationError("confirmed_digest_invalid")
        ref = as_uuid(version_ref)

        version = await self._repository.get_version(actor.user_id, ref)
        if version is None or version.owner_user_id != actor.user_id:
            raise CareerNotFoundError("version_not_found")
        payload = self._read_payload(version)
        digest = profile_digest(
            version.version, payload["profile"], payload["source_refs"]
        )
        if digest != confirmed_digest:
            raise CareerValidationError("confirmation_digest_mismatch")

        existing = await self._repository.get_confirmation_by_idempotency(
            actor.user_id, key
        )
        if existing is not None:
            if (
                existing.version_id != version.id
                or existing.confirmed_digest != digest
            ):
                raise CareerConflictError("idempotency_payload_mismatch")
            return CareerConfirmationView(
                version_ref=version.id,
                version=version.version,
                confirmed_digest=existing.confirmed_digest,
            )

        current = await self._repository.get_confirmation(
            actor.user_id, version.id
        )
        if current is not None:
            raise CareerConflictError("version_already_confirmed")

        row = PersonalCareerProfileConfirmation(
            owner_user_id=actor.user_id,
            version_id=version.id,
            confirmed_digest=digest,
            actor_kind=actor.kind,
            idempotency_key=key,
        )
        await self._repository.add_confirmation(row)
        return CareerConfirmationView(
            version_ref=version.id,
            version=version.version,
            confirmed_digest=digest,
        )

    async def get_profile_version(
        self,
        actor: CareerActor,
        version_ref: Any,
        *,
        guard: Callable[[], Any] | None = None,
    ) -> CareerProfileVersionView:
        """``guard`` (trusted read hook) runs right after each DB await, before payload decryption."""

        ref = as_uuid(version_ref)
        version = await self._repository.get_version(actor.user_id, ref)
        await run_personal_guard(guard)
        if version is None or version.owner_user_id != actor.user_id:
            raise CareerNotFoundError("version_not_found")
        confirmation = await self._repository.get_confirmation(
            actor.user_id, version.id
        )
        await run_personal_guard(guard)
        return self._version_view(version, confirmation)

    async def list_profile_versions(
        self, actor: CareerActor, limit: int = 20
    ) -> tuple[CareerProfileVersionView, ...]:
        """新しい順の版履歴。確認状態も含む（本文は復号して返す）。"""

        limit = _require_bounded_int("limit", limit, 1, 100)
        rows = await self._repository.list_versions_with_confirmation(
            actor.user_id, limit
        )
        return tuple(
            self._version_view(version, confirmation)
            for version, confirmation in rows
        )

    async def list_unconfirmed_claims(
        self, actor: CareerActor, version_ref: Any
    ) -> tuple[UnconfirmedClaim, ...]:
        """未確認クレームの明示参照。context adapter からは決して使わない。"""

        ref = as_uuid(version_ref)
        version = await self._repository.get_version(actor.user_id, ref)
        if version is None or version.owner_user_id != actor.user_id:
            raise CareerNotFoundError("version_not_found")
        payload = self._read_payload(version)
        return tuple(
            UnconfirmedClaim(
                kind=claim["kind"], text=claim["text"], origin=claim["origin"]
            )
            for claim in payload["unconfirmed"]
        )

    # --- context adapter (nonmutating) -------------------------------------

    async def build_context(
        self,
        actor: CareerActor,
        bounds: CareerContextBounds | None = None,
        *,
        accepted_profile_ref: Any | None = None,
        guard: Callable[[], Any] | None = None,
    ) -> CareerContext:
        """確認済み版と選択sourceだけを返す読み取り専用adapter。

        - 書き込み・source登録・確認は一切行わない。
        - 未確認クレームの本文、未選択source、他ownerのデータは含めない。
        - ``accepted_profile_ref`` 指定時はその版だけを使い、最新版へはフォールバックしない。
          missing/他ownerは ``CareerNotFoundError``、未確認/破損/digest不一致は空contextで閉じる。
        """

        effective = bounds or CareerContextBounds()
        chosen: CareerProfileVersionView | None = None
        chosen_has_claims = False

        if accepted_profile_ref is not None:
            try:
                explicit = await self.get_profile_version(
                    actor, accepted_profile_ref, guard=guard
                )
            except CareerValidationError:
                return _empty_context(NEEDS_REVIEW_PAYLOAD_UNREADABLE)
            if not explicit.confirmed:
                return _empty_context(NEEDS_REVIEW_NO_CONFIRMED_PROFILE)
            chosen = explicit
            chosen_has_claims = explicit.unconfirmed_claim_count > 0
            rows = ()
        else:
            rows = await self._repository.list_versions_with_confirmation(
                actor.user_id, CONTEXT_SCAN_VERSIONS
            )
            await run_personal_guard(guard)

        for version, confirmation in rows:
            if version.owner_user_id != actor.user_id:
                continue
            if confirmation is None:
                continue
            try:
                payload = self._read_payload(version)
            except CareerValidationError:
                return _empty_context(NEEDS_REVIEW_PAYLOAD_UNREADABLE)
            digest = profile_digest(
                version.version, payload["profile"], payload["source_refs"]
            )
            if confirmation.confirmed_digest != digest:
                return _empty_context(NEEDS_REVIEW_DIGEST_MISMATCH)
            chosen = self._version_view(version, confirmation)
            chosen_has_claims = bool(payload["unconfirmed"])
            break

        if chosen is None:
            return _empty_context(NEEDS_REVIEW_NO_CONFIRMED_PROFILE)

        selected_sources = await self._repository.list_selected_sources(
            actor.user_id, CONTEXT_HARD_MAX_SOURCES
        )
        await run_personal_guard(guard)
        selected_refs = {row.source_ref for row in selected_sources}
        confirmed_refs = tuple(
            ref for ref in chosen.source_refs if ref in selected_refs
        )

        return assemble_context(
            chosen.profile.to_contract(),
            confirmed_refs,
            has_unconfirmed_claims=chosen_has_claims,
            bounds=effective,
        )

    # --- guards ------------------------------------------------------------

    @staticmethod
    def _require_user_action(actor: CareerActor) -> None:
        if actor.kind != USER_ACTION:
            raise CareerPermissionError("owner_action_required")
