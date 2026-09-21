"""Server-owned provenance ledger for disposable verification data.

The service is intentionally small and conservative. It provides the
allow-list and run context that cleanup code can consume; it never treats a
name, lifecycle state, age, or a client-supplied field as deletion authority.
All writes are bound to a trusted :mod:`src.verification.context` run identity.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models.verification import (
    VERIFICATION_ARTIFACT_CLEANUP_STATUSES,
    VERIFICATION_CLEANUP_ITEM_STATUSES,
    VERIFICATION_CLEANUP_STATUSES,
    VERIFICATION_PROVENANCE_SCHEMA_VERSION,
    VERIFICATION_RUN_STATUSES,
    VerificationArtifactProvenance,
    VerificationCleanupItem,
    VerificationCleanupRun,
    VerificationRun,
)
from ..verification.context import (
    VerificationRunContext,
    build_verification_headers,
    context_from_headers,
    get_current_verification_context,
    get_current_verification_run_id,
    require_current_verification_context,
    reset_current_verification_context,
    signed_verification_context,
    set_current_verification_context,
    verify_verification_headers,
)

logger = logging.getLogger(__name__)

PROVENANCE_METADATA_KEY = "verification_provenance"
LEGACY_PROVENANCE_METADATA_KEYS = ("verification", "qa_provenance")
MAX_RUN_ID_LENGTH = 128
MAX_SOURCE_LENGTH = 255
MAX_ENTITY_TYPE_LENGTH = 64
MAX_ENTITY_ID_LENGTH = 512
MAX_SAFE_METADATA_KEYS = 64
MAX_SAFE_METADATA_DEPTH = 4
MAX_SAFE_METADATA_LIST = 64
_MAX_SAFE_STRING_LENGTH = 1024
_SENSITIVE_KEY_RE = re.compile(
    r"(?:password|passwd|secret|token|credential|authorization|cookie|api[_-]?key|"
    r"prompt|body|content|raw[_-]?error|exception|traceback|stack)",
    re.IGNORECASE,
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class VerificationProvenanceError(ValueError):
    """Raised when a provenance object is missing or attempts authority."""


class VerificationProvenanceConflict(VerificationProvenanceError):
    """Raised when an idempotency identity is reused with different data."""


class VerificationRunNotFound(VerificationProvenanceError):
    """Raised when a write references an unknown verification run."""


def _utcnow() -> datetime:
    return datetime.utcnow()


def _uuid(value: UUID | str, label: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise VerificationProvenanceError(f"{label} must be a UUID") from exc


def _run_id(value: UUID | str, label: str = "run_id") -> UUID:
    return _uuid(value, label)


def _bounded_text(
    value: Any,
    *,
    label: str,
    max_length: int,
    allow_uuid: bool = False,
) -> str:
    # Provenance attribution is a wire/storage contract, not a convenience
    # coercion surface.  Accepting arbitrary objects (for example ``0`` or a
    # list whose string representation happens to be non-empty) would let a
    # caller smuggle an unintended identity through validation.  UUIDs are
    # normalised explicitly at their call sites; all text fields are required
    # to arrive as strings here.
    if allow_uuid and isinstance(value, UUID):
        value = str(value)
    if not isinstance(value, str):
        raise VerificationProvenanceError(f"{label} must be a string")
    text = value.strip()
    if not text:
        raise VerificationProvenanceError(f"{label} is required")
    if len(text) > max_length:
        raise VerificationProvenanceError(f"{label} is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise VerificationProvenanceError(f"{label} contains control characters")
    return text


def _safe_error_code(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace(" ", "_")
    if not text:
        return None
    text = re.sub(r"[^a-z0-9_.:-]", "_", text)
    return text[:96]


def _safe_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy bounded metadata while rejecting sensitive/content-shaped fields."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise VerificationProvenanceError("provenance metadata must be an object")

    def clean(item: Any, depth: int, *, key: str | None = None) -> Any:
        if depth > MAX_SAFE_METADATA_DEPTH:
            raise VerificationProvenanceError("provenance metadata is too deeply nested")
        if key and _SENSITIVE_KEY_RE.search(key):
            raise VerificationProvenanceError(
                f"provenance metadata field {key!r} is not allowed"
            )
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, UUID):
            # UUIDs are a safe, machine-readable identity value for metadata
            # (and are serialized as text for JSON/PostgreSQL portability).
            return str(item)
        if isinstance(item, float):
            if not math.isfinite(item):
                raise VerificationProvenanceError("provenance metadata number is not finite")
            return item
        if isinstance(item, str):
            if len(item) > _MAX_SAFE_STRING_LENGTH:
                raise VerificationProvenanceError("provenance metadata string is too long")
            return item
        if isinstance(item, Mapping):
            if len(item) > MAX_SAFE_METADATA_KEYS:
                raise VerificationProvenanceError("too many provenance metadata fields")
            result: dict[str, Any] = {}
            for raw_key, raw_value in item.items():
                field = _bounded_text(raw_key, label="provenance metadata key", max_length=96)
                result[field] = clean(raw_value, depth + 1, key=field)
            return result
        if isinstance(item, Sequence) and not isinstance(item, (bytes, bytearray)):
            if len(item) > MAX_SAFE_METADATA_LIST:
                raise VerificationProvenanceError("provenance metadata list is too long")
            return [clean(child, depth + 1) for child in item]
        raise VerificationProvenanceError(
            f"unsupported provenance metadata value: {type(item).__name__}"
        )

    cleaned = clean(value, 0)
    if not isinstance(cleaned, dict):  # pragma: no cover - guarded above
        raise VerificationProvenanceError("provenance metadata must be an object")
    return cleaned


def _normalise_status(value: str, allowed: frozenset[str], label: str) -> str:
    status = str(value or "").strip().lower().replace("-", "_")
    if status not in allowed:
        raise VerificationProvenanceError(f"invalid {label}: {status or '<empty>'}")
    return status


def validate_disposable_provenance(
    value: Mapping[str, Any],
    *,
    expected_run_id: UUID | str | None = None,
    expected_source: str | None = None,
    run_id: UUID | str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Validate and canonicalise an entity provenance object.

    ``disposable`` must be the literal boolean ``True`` (not a truthy string),
    the run identity must be a UUID, and source attribution is mandatory. A
    caller-provided timestamp is ignored; durable creation time is assigned by
    the server/model.
    """

    if expected_run_id is None and run_id is not None:
        expected_run_id = run_id
    if expected_source is None and source is not None:
        expected_source = source
    if not isinstance(value, Mapping):
        raise VerificationProvenanceError("disposable provenance must be an object")
    schema_version = value.get("schema_version")
    if type(schema_version) is not int or schema_version != VERIFICATION_PROVENANCE_SCHEMA_VERSION:
        raise VerificationProvenanceError("unsupported provenance schema_version")
    if value.get("disposable") is not True:
        raise VerificationProvenanceError("disposable provenance must set disposable=true")
    run_id = _run_id(value.get("run_id"))
    if expected_run_id is not None and run_id != _run_id(expected_run_id, "expected_run_id"):
        raise VerificationProvenanceError("provenance run_id does not match active run")
    # ``source`` is a required marker field.  Never fall back to ``harness``:
    # accepting a sibling field would let a malformed/partial marker smuggle
    # attribution through validation while omitting the canonical workstream
    # identity.
    if "source" not in value:
        raise VerificationProvenanceError("source is required")
    source = _bounded_text(value["source"], label="source", max_length=MAX_SOURCE_LENGTH)
    if expected_source is not None:
        expected = _bounded_text(expected_source, label="expected_source", max_length=MAX_SOURCE_LENGTH)
        if source != expected:
            raise VerificationProvenanceError("provenance source does not match active run")

    result: dict[str, Any] = {
        "schema_version": VERIFICATION_PROVENANCE_SCHEMA_VERSION,
        "disposable": True,
        "run_id": str(run_id),
        "source": source,
    }
    if "harness" in value:
        harness = value.get("harness")
        result["harness"] = _bounded_text(
            harness, label="harness", max_length=MAX_SOURCE_LENGTH
        )
    # A caller-provided timestamp is deliberately ignored.  The durable
    # run/artifact row's server-assigned ``created_at`` is the only authority
    # for ordering and audit; accepting this field here would let clients
    # backdate or otherwise spoof provenance.
    for key, limit in (("entity_type", MAX_ENTITY_TYPE_LENGTH), ("entity_id", MAX_ENTITY_ID_LENGTH)):
        if key in value:
            normalized = _bounded_text(
                value.get(key),
                label=key,
                max_length=limit,
                allow_uuid=key == "entity_id",
            )
            if key == "entity_type":
                normalized = normalized.casefold().replace("-", "_")
            result[key] = normalized
    # created_at is intentionally omitted: the server-side ledger timestamp is
    # authoritative and cannot be spoofed by a request payload.
    return result


normalise_disposable_provenance = validate_disposable_provenance
validate_provenance = validate_disposable_provenance


def require_disposable_provenance(
    value: Mapping[str, Any],
    *,
    expected_run_id: UUID | str | None = None,
    expected_source: str | None = None,
) -> dict[str, Any]:
    """Validate a marker directly or extract one from entity metadata."""

    candidate: Mapping[str, Any] = value
    if not isinstance(value, Mapping):
        raise VerificationProvenanceError("disposable provenance must be an object")
    if "schema_version" not in value:
        for key in (PROVENANCE_METADATA_KEY, *LEGACY_PROVENANCE_METADATA_KEYS):
            nested = value.get(key)
            if isinstance(nested, Mapping):
                candidate = nested
                break
    return validate_disposable_provenance(
        candidate,
        expected_run_id=expected_run_id,
        expected_source=expected_source,
    )


def build_disposable_provenance(
    *,
    run_id: UUID | str | None = None,
    source: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Build provenance from the trusted current context."""

    context = require_current_verification_context()
    effective_run_id = context.run_id if run_id is None else _run_id(run_id)
    if effective_run_id != context.run_id:
        raise VerificationProvenanceError("run_id does not match active verification context")
    effective_source = context.source if source is None else _bounded_text(
        source, label="source", max_length=MAX_SOURCE_LENGTH
    )
    if effective_source != context.source:
        raise VerificationProvenanceError("source does not match active verification context")
    value: dict[str, Any] = {
        "schema_version": VERIFICATION_PROVENANCE_SCHEMA_VERSION,
        "disposable": True,
        "run_id": str(context.run_id),
        "source": context.source,
        "harness": context.harness or context.source,
        "created_at": context.created_at.isoformat()
        if context.created_at
        else _utcnow().isoformat(),
    }
    if entity_type is not None:
        value["entity_type"] = _bounded_text(
            entity_type, label="entity_type", max_length=MAX_ENTITY_TYPE_LENGTH
        ).casefold().replace("-", "_")
    if entity_id is not None:
        value["entity_id"] = _bounded_text(
            entity_id,
            label="entity_id",
            max_length=MAX_ENTITY_ID_LENGTH,
            allow_uuid=True,
        )
    return value


async def register_current_entity(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: UUID | str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Tag and ledger a newly-created entity under the trusted run context.

    Product writes have no verification context and are returned unchanged.
    A context is only installed by the server-owned signed-header boundary (or
    an explicit harness scope), so a missing durable run is an error rather
    than an untracked disposable artifact.
    """

    context = get_current_verification_context()
    if context is None:
        return None
    tagged = tag_entity_metadata(
        metadata,
        entity_type=entity_type,
        entity_id=str(entity_id),
    )
    await VerificationProvenanceService().register_artifact(
        session,
        run_id=context.run_id,
        entity_type=entity_type,
        entity_id=str(entity_id),
        source=context.source,
        metadata=tagged,
        commit=False,
    )
    return tagged


def tag_entity_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    entity_type: str,
    entity_id: str,
) -> dict[str, Any]:
    """Return metadata with a server-context provenance marker merged in."""

    if metadata is None:
        result: dict[str, Any] = {}
    elif isinstance(metadata, Mapping):
        result = dict(metadata)
    else:
        raise VerificationProvenanceError("entity metadata must be an object")
    # Canonicalise the server-built marker before returning it.  In
    # particular, ``created_at`` from a context is informational only; the
    # durable ORM row's server timestamp is authoritative.  Keeping the same
    # canonical shape in the domain JSON and ledger projection also makes the
    # cleanup metadata re-read deterministic.
    context = require_current_verification_context()
    result[PROVENANCE_METADATA_KEY] = validate_disposable_provenance(
        build_disposable_provenance(entity_type=entity_type, entity_id=entity_id),
        expected_run_id=context.run_id,
        expected_source=context.source,
    )
    return result


def extract_disposable_provenance(
    metadata: Mapping[str, Any] | None,
    *,
    expected_run_id: UUID | str | None = None,
) -> dict[str, Any] | None:
    """Extract a valid marker from entity metadata, failing closed."""

    if not isinstance(metadata, Mapping):
        return None
    candidate = metadata.get(PROVENANCE_METADATA_KEY)
    if candidate is None:
        for key in LEGACY_PROVENANCE_METADATA_KEYS:
            if metadata.get(key) is not None:
                candidate = metadata.get(key)
                break
    if candidate is None:
        return None
    try:
        return validate_disposable_provenance(candidate, expected_run_id=expected_run_id)
    except VerificationProvenanceError:
        return None


class VerificationProvenanceService:
    """Persistence helpers for verification runs and cleanup audit rows."""

    # Keep validation available through the service instance used by route and
    # cleanup coordinators without creating a second policy implementation.
    validate = staticmethod(validate_disposable_provenance)
    # Explicitly named aliases make adapters resilient to the two common
    # spellings used by older verification fixtures.  They all resolve to the
    # same strict validator; there is no weaker compatibility path.
    validate_disposable_provenance = staticmethod(validate_disposable_provenance)
    validate_provenance = staticmethod(validate_disposable_provenance)
    normalise_disposable_provenance = staticmethod(validate_disposable_provenance)
    require_disposable_provenance = staticmethod(require_disposable_provenance)

    async def start_run(
        self,
        session: AsyncSession,
        *,
        run_id: UUID | str | None = None,
        source: str | None = None,
        harness: str | None = None,
        actor_user_id: UUID | str | None = None,
        metadata: Mapping[str, Any] | None = None,
        run_metadata: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> VerificationRun:
        # ``None`` requests a server-generated UUID; any explicitly supplied
        # value (including an empty string/zero) must pass strict UUID
        # validation rather than silently triggering a fresh run.
        effective_run_id = _run_id(uuid.uuid4() if run_id is None else run_id)
        bound_context = get_current_verification_context()
        context_matches = (
            bound_context is not None
            and bound_context.run_id == effective_run_id
        )
        # Treat an explicitly supplied empty field as malformed rather than
        # silently falling back to its sibling attribution field.
        if source is None and harness is None and context_matches:
            source = bound_context.source
        if harness is None and context_matches:
            harness = bound_context.harness
        if context_matches:
            if source is not None and _bounded_text(
                source,
                label="source",
                max_length=MAX_SOURCE_LENGTH,
            ) != bound_context.source:
                raise VerificationProvenanceError(
                    "source does not match active verification context"
                )
            if harness is not None and _bounded_text(
                harness,
                label="harness",
                max_length=MAX_SOURCE_LENGTH,
            ) != bound_context.harness:
                raise VerificationProvenanceError(
                    "harness does not match active verification context"
                )
        # ``source`` is the canonical workstream attribution and is required
        # independently of the optional operator-facing harness label.  Do
        # not silently promote ``harness`` into ``source`` when a caller omits
        # the canonical field; doing so would let a malformed/partial marker
        # lose its stable origin.
        if source is None:
            raise VerificationProvenanceError("source is required")
        effective_source = _bounded_text(
            source,
            label="source",
            max_length=MAX_SOURCE_LENGTH,
        )
        effective_harness = _bounded_text(
            harness if harness is not None else effective_source,
            label="harness",
            max_length=MAX_SOURCE_LENGTH,
        )
        safe_metadata = _safe_metadata(metadata if metadata is not None else run_metadata)
        run_marker = safe_metadata.get(PROVENANCE_METADATA_KEY)
        if run_marker is not None:
            canonical_marker = validate_disposable_provenance(
                run_marker,
                expected_run_id=effective_run_id,
                expected_source=effective_source,
            )
            if canonical_marker.get("harness") not in (None, effective_harness):
                raise VerificationProvenanceError(
                    "run provenance harness does not match requested harness"
                )
            canonical_marker.setdefault("harness", effective_harness)
            safe_metadata[PROVENANCE_METADATA_KEY] = canonical_marker
        else:
            # Persist a canonical run-level marker even when the caller did
            # not supply arbitrary metadata.  The durable columns remain the
            # authority, while this bounded sidecar lets operators and
            # generic repository tooling group rows without inspecting names.
            safe_metadata[PROVENANCE_METADATA_KEY] = {
                "schema_version": VERIFICATION_PROVENANCE_SCHEMA_VERSION,
                "disposable": True,
                "run_id": str(effective_run_id),
                "source": effective_source,
                "harness": effective_harness,
            }
        actor_uuid = _uuid(actor_user_id, "actor_user_id") if actor_user_id is not None else None

        existing = await session.scalar(
            select(VerificationRun).where(VerificationRun.run_id == effective_run_id)
        )
        if existing is not None:
            self._assert_run_row(existing, effective_source)
            # Rows created before ``harness`` became explicit are treated as
            # using their source as the effective harness.  A retry that
            # presents a different harness must not silently reuse that run
            # identity, even when the legacy row stored NULL.
            existing_harness = existing.harness or existing.source
            if existing_harness != effective_harness:
                raise VerificationProvenanceConflict("run_id is already bound to another harness")
            return existing

        now = _utcnow()
        run = VerificationRun(
            run_id=effective_run_id,
            schema_version=VERIFICATION_PROVENANCE_SCHEMA_VERSION,
            disposable=True,
            source=effective_source,
            harness=effective_harness,
            actor_user_id=actor_uuid,
            status="running",
            metadata_json=safe_metadata,
            created_at=now,
            started_at=now,
            updated_at=now,
        )
        try:
            async with session.begin_nested():
                session.add(run)
                await session.flush()
        except IntegrityError:
            existing = await session.scalar(
                select(VerificationRun).where(VerificationRun.run_id == effective_run_id)
            )
            if existing is None:
                raise
            self._assert_run_row(existing, effective_source)
            existing_harness = existing.harness or existing.source
            if existing_harness != effective_harness:
                raise VerificationProvenanceConflict("run_id is already bound to another harness")
            return existing
        if commit:
            await session.commit()
        return run

    create_run = start_run
    open_run = start_run

    @staticmethod
    def _assert_run_row(run: VerificationRun, source: str | None = None) -> None:
        if run.disposable is not True:
            raise VerificationProvenanceError("verification run is not disposable")
        if run.schema_version != VERIFICATION_PROVENANCE_SCHEMA_VERSION:
            raise VerificationProvenanceError("unsupported verification run schema")
        _normalise_status(run.status, VERIFICATION_RUN_STATUSES, "run status")
        if source is not None and str(run.source or "").strip() != source:
            raise VerificationProvenanceConflict("run_id is already bound to another source")

    async def get_run(self, session: AsyncSession, run_id: UUID | str) -> VerificationRun | None:
        return await session.scalar(
            select(VerificationRun).where(VerificationRun.run_id == _run_id(run_id))
        )

    async def list_runs(
        self,
        session: AsyncSession,
        *,
        limit: int = 100,
        include_cleaned: bool = True,
    ) -> list[VerificationRun]:
        bounded_limit = max(1, min(int(limit), 500))
        query = select(VerificationRun).where(VerificationRun.disposable.is_(True))
        if not include_cleaned:
            query = query.where(VerificationRun.status.not_in(("cleaned",)))
        query = query.order_by(desc(VerificationRun.created_at)).limit(bounded_limit)
        return list((await session.scalars(query)).all())

    async def register_artifact(
        self,
        session: AsyncSession,
        *,
        run_id: UUID | str,
        entity_type: str,
        entity_id: str,
        disposable: bool = True,
        source: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> VerificationArtifactProvenance:
        context = require_current_verification_context()
        effective_run_id = _run_id(run_id)
        if effective_run_id != context.run_id:
            raise VerificationProvenanceError("artifact run_id does not match active context")
        if disposable is not True:
            raise VerificationProvenanceError("verification artifacts must be disposable=true")
        normalized_type = _bounded_text(entity_type, label="entity_type", max_length=MAX_ENTITY_TYPE_LENGTH).casefold().replace("-", "_")
        normalized_id = _bounded_text(
            entity_id,
            label="entity_id",
            max_length=MAX_ENTITY_ID_LENGTH,
            allow_uuid=True,
        )
        canonical_provenance = None
        if provenance is not None:
            canonical_provenance = validate_disposable_provenance(
                provenance,
                expected_run_id=context.run_id,
                expected_source=context.source,
            )
        effective_source = context.source if source is None else _bounded_text(source, label="source", max_length=MAX_SOURCE_LENGTH)
        if effective_source != context.source:
            raise VerificationProvenanceError("artifact source does not match active context")
        run = await self.get_run(session, effective_run_id)
        if run is None:
            raise VerificationRunNotFound(str(effective_run_id))
        self._assert_run_row(run, context.source)
        if run.harness and run.harness != context.harness:
            raise VerificationProvenanceConflict(
                "artifact harness does not match durable verification run"
            )
        if run.status != "running":
            raise VerificationProvenanceConflict("verification run is no longer active")
        safe_metadata = _safe_metadata(metadata)
        # Metadata is an attribution surface, never deletion authority.  If a
        # caller supplied a marker, validate it against the server-owned
        # ContextVar and canonicalise it (dropping caller timestamps).  If no
        # marker was supplied, attach one from the trusted context so every
        # verification artifact remains machine-attributable without relying
        # on a human-readable name.
        metadata_marker = safe_metadata.get(PROVENANCE_METADATA_KEY)
        if canonical_provenance is None and metadata_marker is not None:
            canonical_provenance = validate_disposable_provenance(
                metadata_marker,
                expected_run_id=context.run_id,
                expected_source=context.source,
            )
        if canonical_provenance is None:
            canonical_provenance = validate_disposable_provenance(
                build_disposable_provenance(
                    run_id=context.run_id,
                    source=context.source,
                    entity_type=normalized_type,
                    entity_id=normalized_id,
                ),
                expected_run_id=context.run_id,
                expected_source=context.source,
            )
        if canonical_provenance.get("entity_type") not in (None, normalized_type):
            raise VerificationProvenanceError("provenance entity_type does not match artifact")
        if canonical_provenance.get("entity_id") not in (None, normalized_id):
            raise VerificationProvenanceError("provenance entity_id does not match artifact")
        if canonical_provenance.get("harness") not in (None, context.harness):
            raise VerificationProvenanceError("provenance harness does not match active context")
        canonical_provenance.setdefault("harness", context.harness or context.source)
        # Complete the server-owned identity even when a caller supplied a
        # minimal marker containing only run/source fields.  The caller cannot
        # choose a conflicting identity (the equality fences above reject
        # that), while generic metadata readers still get a self-contained
        # entity marker.
        canonical_provenance.setdefault("entity_type", normalized_type)
        canonical_provenance.setdefault("entity_id", normalized_id)
        safe_metadata[PROVENANCE_METADATA_KEY] = canonical_provenance

        existing = await session.scalar(
            select(VerificationArtifactProvenance).where(
                VerificationArtifactProvenance.run_id == effective_run_id,
                VerificationArtifactProvenance.entity_type == normalized_type,
                VerificationArtifactProvenance.entity_id == normalized_id,
            )
        )
        if existing is not None:
            if existing.source != effective_source or existing.disposable is not True:
                raise VerificationProvenanceConflict("artifact identity is already bound to different provenance")
            return existing

        now = _utcnow()
        artifact = VerificationArtifactProvenance(
            run_id=effective_run_id,
            schema_version=VERIFICATION_PROVENANCE_SCHEMA_VERSION,
            disposable=True,
            entity_type=normalized_type,
            entity_id=normalized_id,
            source=effective_source,
            metadata_json=safe_metadata,
            cleanup_status="pending",
            created_at=now,
            updated_at=now,
        )
        try:
            # Isolate the unique-identity insert in a SAVEPOINT.  A
            # concurrent retry that loses the race must not roll back
            # unrelated domain rows in the caller's transaction.
            async with session.begin_nested():
                session.add(artifact)
                await session.flush()
        except IntegrityError:
            existing = await session.scalar(
                select(VerificationArtifactProvenance).where(
                    VerificationArtifactProvenance.run_id == effective_run_id,
                    VerificationArtifactProvenance.entity_type == normalized_type,
                    VerificationArtifactProvenance.entity_id == normalized_id,
                )
            )
            if existing is None:
                raise
            return existing
        if commit:
            await session.commit()
        return artifact

    attach_artifact = register_artifact
    attach = register_artifact
    register = register_artifact

    async def list_artifacts(
        self,
        session: AsyncSession,
        run_id: UUID | str,
        *,
        include_cleaned: bool = True,
    ) -> list[VerificationArtifactProvenance]:
        query = select(VerificationArtifactProvenance).where(
            VerificationArtifactProvenance.run_id == _run_id(run_id)
        )
        if not include_cleaned:
            query = query.where(
                VerificationArtifactProvenance.cleanup_status.in_(("pending", "failed"))
            )
        query = query.order_by(VerificationArtifactProvenance.created_at)
        return list((await session.scalars(query)).all())

    async def finish_run(
        self,
        session: AsyncSession,
        run_id: UUID | str,
        *,
        status: str = "succeeded",
        error_code: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> VerificationRun:
        # ``completed`` was used by an early coordinator adapter; canonical
        # durable rows use ``succeeded`` while retaining that spelling as an
        # input compatibility alias.
        normalized_status = _normalise_status(
            {"completed": "succeeded", "success": "succeeded", "idempotent": "cleaned"}.get(
                str(status).strip().lower(), str(status)
            ),
            VERIFICATION_RUN_STATUSES,
            "run status",
        )
        run = await self.get_run(session, run_id)
        if run is None:
            raise VerificationRunNotFound(str(run_id))
        self._assert_run_row(run)
        if run.status in {"succeeded", "failed", "aborted", "cleaned", "cleanup_failed"}:
            if run.status == normalized_status:
                return run
            # A successful/failed execution may transition once to
            # ``cleaned`` after the canonical domain purge. No other terminal
            # state can be rewritten or reopened.
            can_close_after_cleanup = normalized_status == "cleaned" and run.status in {
                "succeeded",
                "failed",
                "aborted",
                "cleanup_failed",
            }
            can_mark_cleanup_failed = normalized_status == "cleanup_failed" and run.status in {
                "succeeded",
                "failed",
                "aborted",
            }
            if not (can_close_after_cleanup or can_mark_cleanup_failed):
                raise VerificationProvenanceConflict("verification run is already terminal")
        run.status = normalized_status
        run.completed_at = run.completed_at or _utcnow()
        run.updated_at = _utcnow()
        if metadata is not None:
            updated_metadata = _safe_metadata(metadata)
            marker = updated_metadata.get(PROVENANCE_METADATA_KEY)
            if marker is not None:
                canonical_marker = validate_disposable_provenance(
                    marker,
                    expected_run_id=run.run_id,
                    expected_source=run.source,
                )
                if canonical_marker.get("harness") not in (None, run.harness):
                    raise VerificationProvenanceError(
                        "run provenance harness does not match durable run"
                    )
                canonical_marker.setdefault("harness", run.harness or run.source)
                updated_metadata[PROVENANCE_METADATA_KEY] = canonical_marker
            else:
                # Preserve the durable run marker when callers only append a
                # completion summary; replacing metadata must not erase the
                # machine-readable allow-list identity.
                updated_metadata[PROVENANCE_METADATA_KEY] = {
                    "schema_version": VERIFICATION_PROVENANCE_SCHEMA_VERSION,
                    "disposable": True,
                    "run_id": str(run.run_id),
                    "source": run.source,
                    "harness": run.harness or run.source,
                }
            run.metadata_json = updated_metadata
        if error_code:
            merged = dict(run.metadata_json or {})
            merged["error_code"] = _safe_error_code(error_code)
            run.metadata_json = merged
        if commit:
            await session.commit()
        return run

    mark_run_completed = finish_run
    complete_run = finish_run

    async def mark_run_failed(
        self,
        session: AsyncSession,
        run_id: UUID | str,
        *,
        error_code: str | None = None,
        commit: bool = True,
    ) -> VerificationRun:
        return await self.finish_run(session, run_id, status="failed", error_code=error_code, commit=commit)

    async def preview_run(self, session: AsyncSession, run_id: UUID | str) -> dict[str, Any]:
        run = await self.get_run(session, run_id)
        if run is None:
            raise VerificationRunNotFound(str(run_id))
        self._assert_run_row(run)
        artifacts = await self.list_artifacts(session, run.run_id)
        counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        for artifact in artifacts:
            counts[artifact.entity_type] = counts.get(artifact.entity_type, 0) + 1
            status_counts[artifact.cleanup_status] = status_counts.get(artifact.cleanup_status, 0) + 1
        return {
            "run": run.to_dict(),
            "counts": counts,
            "status_counts": status_counts,
            "total_artifacts": len(artifacts),
            "artifacts": [artifact.to_dict() for artifact in artifacts],
        }

    async def build_cleanup_manifest(
        self,
        session: AsyncSession,
        run_id: UUID | str,
    ) -> dict[str, Any]:
        """Project a durable run/artifact ledger into a cleanup manifest.

        Only the three entity types for which the coordinator has canonical
        deletion semantics are emitted.  Other artifacts (files, external
        jobs, etc.) remain visible in ``preview_run`` but cannot accidentally
        be interpreted as ORM identities by this cleanup surface.
        """

        preview = await self.preview_run(session, run_id)
        run = preview["run"]
        entities: list[dict[str, Any]] = []
        for artifact in preview.get("artifacts", []):
            if not isinstance(artifact, Mapping):
                continue
            entity_type = str(artifact.get("entity_type") or "").casefold()
            if entity_type not in {"project", "task", "user"}:
                continue
            entity_id = str(artifact.get("entity_id") or "").strip()
            if not entity_id:
                continue
            metadata = artifact.get("metadata")
            entities.append(
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "category": "A",
                    "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
                }
            )
        if not entities:
            raise VerificationProvenanceError("verification run has no deletable artifacts")
        return {
            "schema_version": VERIFICATION_PROVENANCE_SCHEMA_VERSION,
            "manifest_id": str(run["run_id"]),
            "run_id": str(run["run_id"]),
            "source": str(run.get("source") or run.get("harness") or "verification"),
            "disposable": True,
            "created_at": run.get("created_at"),
            "entities": entities,
            "metadata": run.get("metadata") if isinstance(run.get("metadata"), Mapping) else {},
        }

    manifest_for_run = build_cleanup_manifest

    async def list_eligible(
        self,
        session: AsyncSession,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        """List only disposable runs that still have pending artifacts."""

        selectors: list[dict[str, Any]] = []
        runs: list[dict[str, Any]] = []
        # Include a nominally cleaned run when an interrupted coordinator left
        # pending/failed artifact rows behind; this preserves a safe retry path
        # without ever selecting a run whose entire artifact set is terminal.
        for run in await self.list_runs(session, limit=limit, include_cleaned=True):
            artifacts = await self.list_artifacts(session, run.run_id, include_cleaned=False)
            if not artifacts:
                continue
            selector = {
                "type": "verification_run",
                "id": str(run.run_id),
                "classification": "A",
                "category": "A",
                "source": run.source,
                "harness": run.harness or run.source,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "status": run.status,
                "counts": {
                    entity_type: sum(1 for artifact in artifacts if artifact.entity_type == entity_type)
                    for entity_type in sorted({artifact.entity_type for artifact in artifacts})
                },
            }
            selectors.append(selector)
            runs.append(run.to_dict())
        selectors.sort(key=lambda item: (str(item.get("id")), str(item.get("type"))))
        return {
            "status": "preview",
            "selectors": selectors,
            "runs": runs,
            "counts": {
                "runs": len(selectors),
                "artifacts": sum(sum(item.get("counts", {}).values()) for item in selectors),
            },
            "preview_digest": hashlib.sha256(
                json.dumps(selectors, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }

    async def get_cleanup(
        self,
        session: AsyncSession,
        *,
        run_id: UUID | str,
    ) -> dict[str, Any] | None:
        """Return the latest cleanup ledger projection, if one exists."""

        try:
            run_uuid = _run_id(run_id)
        except VerificationProvenanceError:
            # Legacy manifests use opaque keys and are recorded in the
            # append-only ContentDeletionEvent fallback instead.
            return None
        row = await session.scalar(
            select(VerificationCleanupRun)
            .where(VerificationCleanupRun.run_id == run_uuid)
            .order_by(desc(VerificationCleanupRun.created_at))
            .limit(1)
        )
        if row is None:
            return None
        return {
            "id": str(row.id),
            "run_id": str(row.run_id),
            "cleanup_id": str(row.id),
            "status": row.status,
            "manifest_digest": row.confirmation_sha256,
            "digest": row.confirmation_sha256,
            "preview_digest": (row.metadata_json or {}).get("preview_digest"),
            "counts": row.summary_json or {},
            "graph": (row.metadata_json or {}).get("graph", {}),
            "deleted": (row.metadata_json or {}).get("deleted", {}),
        }

    async def create_cleanup_run(
        self,
        session: AsyncSession,
        *,
        run_id: UUID | str,
        actor_user_id: UUID | str | None = None,
        confirmation_sha256: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> VerificationCleanupRun:
        run = await self.get_run(session, run_id)
        if run is None:
            raise VerificationRunNotFound(str(run_id))
        self._assert_run_row(run)
        actor_uuid = _uuid(actor_user_id, "actor_user_id") if actor_user_id is not None else None
        digest = self._normalise_digest(confirmation_sha256)
        now = _utcnow()
        cleanup = VerificationCleanupRun(
            run_id=run.run_id,
            actor_user_id=actor_uuid,
            status="running",
            confirmation_sha256=digest,
            metadata_json=_safe_metadata(metadata),
            summary_json={},
            created_at=now,
            started_at=now,
            updated_at=now,
        )
        session.add(cleanup)
        await session.flush()
        if commit:
            await session.commit()
        return cleanup

    begin_cleanup = create_cleanup_run

    @staticmethod
    def _normalise_digest(value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        if text.startswith("sha256:"):
            text = text[7:]
        if _SHA256_RE.fullmatch(text):
            return text
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    async def record_cleanup_item(
        self,
        session: AsyncSession,
        *,
        cleanup_run_id: UUID | str,
        run_id: UUID | str,
        entity_type: str,
        entity_id: str,
        status: str,
        artifact_id: UUID | str | None = None,
        safe_error_code: str | None = None,
        result: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> VerificationCleanupItem:
        normalized_status = _normalise_status(status, VERIFICATION_CLEANUP_ITEM_STATUSES, "cleanup item status")
        cleanup_uuid = _uuid(cleanup_run_id, "cleanup_run_id")
        run_uuid = _run_id(run_id)
        normalized_type = _bounded_text(entity_type, label="entity_type", max_length=MAX_ENTITY_TYPE_LENGTH).casefold().replace("-", "_")
        normalized_id = _bounded_text(
            entity_id,
            label="entity_id",
            max_length=MAX_ENTITY_ID_LENGTH,
            allow_uuid=True,
        )
        artifact_uuid = _uuid(artifact_id, "artifact_id") if artifact_id is not None else None
        cleanup = await session.get(VerificationCleanupRun, cleanup_uuid)
        if cleanup is None:
            raise VerificationProvenanceError("cleanup run not found")
        if cleanup.run_id != run_uuid:
            raise VerificationProvenanceError("cleanup item run_id does not match cleanup run")
        if artifact_uuid is not None:
            artifact = await session.get(VerificationArtifactProvenance, artifact_uuid)
            if artifact is None:
                raise VerificationProvenanceError("verification artifact not found")
            if artifact.run_id != run_uuid:
                raise VerificationProvenanceError("cleanup item artifact belongs to another run")
            if artifact.entity_type != normalized_type or artifact.entity_id != normalized_id:
                raise VerificationProvenanceError(
                    "cleanup item artifact identity does not match"
                )
        existing = await session.scalar(
            select(VerificationCleanupItem).where(
                VerificationCleanupItem.cleanup_run_id == cleanup_uuid,
                VerificationCleanupItem.entity_type == normalized_type,
                VerificationCleanupItem.entity_id == normalized_id,
            )
        )
        if existing is not None:
            return existing
        item = VerificationCleanupItem(
            cleanup_run_id=cleanup_uuid,
            artifact_id=artifact_uuid,
            run_id=run_uuid,
            entity_type=normalized_type,
            entity_id=normalized_id,
            status=normalized_status,
            safe_error_code=_safe_error_code(safe_error_code),
            result_json=_safe_metadata(result),
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        try:
            async with session.begin_nested():
                session.add(item)
                await session.flush()
        except IntegrityError:
            # Concurrent retries may race the unique cleanup-item identity;
            # resolve that race to the already durable row rather than
            # emitting a duplicate or failing a safe retry.
            existing = await session.scalar(
                select(VerificationCleanupItem).where(
                    VerificationCleanupItem.cleanup_run_id == cleanup_uuid,
                    VerificationCleanupItem.entity_type == normalized_type,
                    VerificationCleanupItem.entity_id == normalized_id,
                )
            )
            if existing is None:
                raise
            return existing
        if commit:
            await session.commit()
        return item

    async def finish_cleanup(
        self,
        session: AsyncSession,
        cleanup_run_id: UUID | str,
        *,
        status: str,
        counts: Mapping[str, Any] | None = None,
        error: str | None = None,
        details: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> VerificationCleanupRun:
        # Keep the ledger vocabulary compact while accepting the coordinator's
        # historical ``completed`` spelling at the service boundary.
        normalized_status = _normalise_status(
            {"completed": "succeeded", "success": "succeeded", "idempotent": "already_clean"}.get(
                str(status).strip().lower(), str(status)
            ),
            VERIFICATION_CLEANUP_STATUSES,
            "cleanup status",
        )
        cleanup = await session.get(VerificationCleanupRun, _uuid(cleanup_run_id, "cleanup_run_id"))
        if cleanup is None:
            raise VerificationProvenanceError("cleanup run not found")
        if cleanup.status != "running":
            # ``succeeded`` and ``already_clean`` are equivalent terminal
            # outcomes for idempotent coordinator retries; likewise a
            # ``partial``/``failed`` retry may report either failure label
            # without rewriting the original audit row.
            equivalent_terminal = (
                {cleanup.status, normalized_status} <= {"succeeded", "already_clean"}
                or {cleanup.status, normalized_status} <= {"failed", "partial"}
            )
            if cleanup.status != normalized_status and not equivalent_terminal:
                raise VerificationProvenanceConflict("cleanup run is already terminal")
            return cleanup
        cleanup.status = normalized_status
        cleanup.completed_at = cleanup.completed_at or _utcnow()
        cleanup.updated_at = _utcnow()
        summary: dict[str, Any] = _safe_metadata(details)
        if counts is not None:
            for key, value in counts.items():
                field = _bounded_text(key, label="cleanup count key", max_length=64)
                try:
                    number = max(0, int(value))
                except (TypeError, ValueError):
                    continue
                summary[field] = number
        if error:
            summary["error_code"] = _safe_error_code(error)
        cleanup.summary_json = summary
        # A successful cleanup closes the associated verification scope.  This
        # is kept in the same transaction as the cleanup ledger row so a
        # preview cannot observe a purged artifact set under an open run.
        if normalized_status in {"succeeded", "already_clean"}:
            parent_run = await self.get_run(session, cleanup.run_id)
            if parent_run is not None and parent_run.status != "cleaned":
                await self.finish_run(
                    session,
                    parent_run.run_id,
                    status="cleaned",
                    commit=False,
                )
        elif normalized_status in {"failed", "partial"}:
            parent_run = await self.get_run(session, cleanup.run_id)
            if parent_run is not None and parent_run.status != "cleaned":
                # Preserve a durable distinction between an execution failure
                # and a cleanup failure while keeping a subsequent successful
                # retry eligible for the explicit ``cleaned`` transition.
                await self.finish_run(
                    session,
                    parent_run.run_id,
                    status="cleanup_failed",
                    error_code=error or "verification_cleanup_failed",
                    commit=False,
                )
        if commit:
            await session.commit()
        return cleanup

    async def record_cleanup(
        self,
        session: AsyncSession,
        run_id: UUID | str,
        digest: str | None = None,
        status: str = "already_clean",
        counts: Mapping[str, Any] | None = None,
        actor_user_id: UUID | str | None = None,
        error: str | None = None,
        *,
        manifest_digest: str | None = None,
        details: Mapping[str, Any] | None = None,
        source: str | None = None,
        commit: bool = True,
    ) -> VerificationCleanupRun:
        # ``completed`` is an older call-site spelling for a successful
        # cleanup; persist the canonical ``succeeded`` status instead.
        status = {"completed": "succeeded", "success": "succeeded", "idempotent": "already_clean"}.get(
            str(status).strip().lower(), str(status)
        )
        if source is not None:
            loaded = await self.get_run(session, run_id)
            if loaded is None:
                raise VerificationRunNotFound(str(run_id))
            self._assert_run_row(
                loaded,
                _bounded_text(source, label="source", max_length=MAX_SOURCE_LENGTH),
            )
        cleanup = await self.create_cleanup_run(
            session,
            run_id=run_id,
            actor_user_id=actor_user_id,
            confirmation_sha256=manifest_digest or digest,
            metadata=details,
            commit=False,
        )
        if status in {"succeeded", "already_clean"}:
            loaded = await self.get_run(session, run_id)
            if loaded is not None and loaded.status != "cleaned":
                await self.finish_run(session, loaded.run_id, status="cleaned", commit=False)
        return await self.finish_cleanup(
            session,
            cleanup.id,
            status=status,
            counts=counts,
            error=error,
            commit=commit,
        )

    async def cleanup_run(
        self,
        session: AsyncSession,
        run_id: UUID | str,
        *,
        actor_user_id: UUID | str | None = None,
        confirmation_sha256: str | None = None,
        status: str = "already_clean",
        counts: Mapping[str, Any] | None = None,
        error: str | None = None,
        source: str | None = None,
        details: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> VerificationCleanupRun:
        """Record an already-performed cleanup; does not delete domain rows."""

        return await self.record_cleanup(
            session,
            run_id,
            confirmation_sha256,
            status,
            counts,
            actor_user_id=actor_user_id,
            error=error,
            source=source,
            details=details,
            commit=commit,
        )

    async def mark_artifact_cleaned(
        self,
        session: AsyncSession,
        artifact_id: UUID | str,
        *,
        status: str = "deleted",
        error_code: str | None = None,
        commit: bool = True,
    ) -> VerificationArtifactProvenance:
        normalized_status = _normalise_status(status, VERIFICATION_ARTIFACT_CLEANUP_STATUSES, "artifact cleanup status")
        artifact = await session.get(VerificationArtifactProvenance, _uuid(artifact_id, "artifact_id"))
        if artifact is None:
            raise VerificationProvenanceError("verification artifact not found")
        if artifact.cleanup_status in {"deleted", "already_absent", "skipped"}:
            # A retry must not rewrite a successful terminal result into a
            # different label merely because the domain row is now absent.
            if artifact.cleanup_status != normalized_status:
                return artifact
            return artifact
        artifact.cleanup_status = normalized_status
        artifact.cleanup_error_code = _safe_error_code(error_code)
        artifact.cleaned_at = artifact.cleaned_at or _utcnow()
        artifact.updated_at = _utcnow()
        if commit:
            await session.commit()
        return artifact

    @asynccontextmanager
    async def verification_run_context(
        self,
        session: AsyncSession,
        *,
        source: str,
        run_id: UUID | str | None = None,
        harness: str | None = None,
        actor_user_id: UUID | str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[VerificationRun]:
        """Async context that opens/finalizes a run and restores ContextVar."""

        run = await self.start_run(
            session,
            run_id=run_id,
            source=source,
            harness=harness,
            actor_user_id=actor_user_id,
            metadata=metadata,
        )
        if run.status != "running":
            # A terminal run is immutable (except the explicit cleanup
            # transition) and must never be reused as a write context.
            raise VerificationProvenanceConflict("verification run is no longer active")
        token = set_current_verification_context(
            VerificationRunContext(
                run_id=run.run_id,
                source=run.source,
                harness=run.harness,
                created_at=run.created_at,
            )
        )
        try:
            yield run
        except BaseException as exc:
            try:
                await self.finish_run(
                    session,
                    run.run_id,
                    status="failed",
                    error_code=type(exc).__name__,
                )
            except Exception:
                logger.exception("failed to finalize verification run after exception")
            raise
        else:
            await self.finish_run(session, run.run_id, status="succeeded")
        finally:
            reset_current_verification_context(token)


def _service() -> VerificationProvenanceService:
    return VerificationProvenanceService()


async def create_verification_run(*args: Any, **kwargs: Any) -> VerificationRun:
    return await _service().start_run(*args, **kwargs)


start_verification_run = create_verification_run


async def attach_verification_artifact(*args: Any, **kwargs: Any) -> VerificationArtifactProvenance:
    return await _service().register_artifact(*args, **kwargs)


register_verification_artifact = attach_verification_artifact


def verification_run_context(*args: Any, **kwargs: Any):
    """Module-level async-context wrapper for fixture/harness callers."""

    return _service().verification_run_context(*args, **kwargs)


__all__ = [
    "LEGACY_PROVENANCE_METADATA_KEYS",
    "PROVENANCE_METADATA_KEY",
    "VerificationProvenanceConflict",
    "VerificationProvenanceError",
    "VerificationProvenanceService",
    "VerificationRunNotFound",
    "attach_verification_artifact",
    "build_disposable_provenance",
    "build_verification_headers",
    "context_from_headers",
    "create_verification_run",
    "extract_disposable_provenance",
    "get_current_verification_context",
    "get_current_verification_run_id",
    "normalise_disposable_provenance",
    "register_verification_artifact",
    "register_current_entity",
    "require_disposable_provenance",
    "require_current_verification_context",
    "reset_current_verification_context",
    "set_current_verification_context",
    "signed_verification_context",
    "tag_entity_metadata",
    "validate_disposable_provenance",
    "validate_provenance",
    "verify_verification_headers",
    "verification_run_context",
    "start_verification_run",
]
