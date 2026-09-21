"""Authoritative scope-safe memory pipeline.

All durable memory mutations, including Dreaming and correction handling, pass
through this service. Retrieval methods never commit or update usage fields.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
import threading
import unicodedata
import uuid
from collections.abc import Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Optional

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import aliased

from ..memory.database import get_db_session
from ..memory.models import (
    ContextMemory,
    ContextMemoryAudit,
    ConversationMessage,
    ConversationSession,
    KnowledgeNode,
    KnowledgeNodeShare,
    KnowledgeRevision,
    ProjectKnowledgeRef,
    Project,
    ProjectMember,
    ScopedMemoryPrincipal,
    ScopedMemoryJob,
    DreamingMemoryRun,
    DreamingMemoryState,
    Task,
    TaskActivity,
    User,
)
from .docs_acl import docs_readable_node_predicate
from .project_permissions import normalize_project_member_permissions
from .privacy_masking_projection import is_privacy_masking_source
from .task_project_invariants import lock_task_project_ids
logger = logging.getLogger(__name__)


VALID_SCOPES = ("global", "user", "project", "task", "session")
SCOPE_PRIORITY = {"global": 1, "user": 2, "project": 3, "task": 4, "session": 5}
ACTIVE_STATUSES = {"active", "candidate"}
_DEDUPE_LOCKS: dict[str, tuple[asyncio.Lock, int]] = {}
_DEDUPE_LOCKS_GUARD = threading.Lock()

_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_\-]{3,}")
_CJK_RUN_RE = re.compile(r"[ぁ-んァ-ン一-龥]{2,}")
_ALWAYS_ON_MEMORY_TYPES = {"preference", "constraint", "instruction"}
_CJK_STOP_BIGRAMS = {
    "して",
    "する",
    "した",
    "いる",
    "ある",
    "ます",
    "です",
    "こと",
    "ため",
    "よう",
    "から",
    "ので",
    "これ",
    "それ",
}


def _keywords(text: Optional[str]) -> set[str]:
    if not text:
        return set()
    raw = str(text)
    terms = {part.casefold() for part in _ASCII_WORD_RE.findall(raw)}
    for run in _CJK_RUN_RE.findall(raw):
        clipped = run[:64]
        terms.add(clipped.casefold())
        for size in (2, 3, 4):
            if len(clipped) < size:
                continue
            for index in range(0, len(clipped) - size + 1):
                term = clipped[index : index + size].casefold()
                if size == 2 and term in _CJK_STOP_BIGRAMS:
                    continue
                terms.add(term)
                if len(terms) >= 120:
                    return terms
    return terms


def _memory_selection(
    item: dict[str, Any],
    *,
    terms: set[str],
    project_id: Optional[str],
    task_id: Optional[str],
    session_id: Optional[str],
) -> tuple[bool, str, int]:
    """Return whether a scoped memory is useful for this specific turn."""
    haystack = (
        f"{item.get('title') or ''}\n{item.get('content') or ''}".casefold()
    )
    keyword_score = sum(1 for term in terms if term in haystack)
    if item.get("is_pinned"):
        return True, "pinned", keyword_score
    if keyword_score:
        return True, "current_message_keyword_match", keyword_score

    memory_type = str(item.get("memory_type") or "").casefold()
    importance = int(item.get("importance") or 0)
    if memory_type in _ALWAYS_ON_MEMORY_TYPES and importance >= 8:
        return True, "high_importance_user_guidance", 0
    if (
        importance >= 7
        and (
            (session_id and item.get("session_id") == session_id)
            or (task_id and item.get("task_id") == task_id)
        )
    ):
        return True, "active_session_or_task_scope", 0
    return False, "not_relevant_to_current_turn", 0


_SECRET_RE = re.compile(
    r"(?:password|passwd|secret|bearer\s+[a-z0-9._-]+|api[_ -]?key|"
    r"token\s*[:=]|秘密鍵|パスワード\s*[:：])",
    re.IGNORECASE,
)
_SENSITIVE_RE = re.compile(
    r"(?:\b\d{3}-\d{2}-\d{4}\b|\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b|"
    r"住所|電話番号|生年月日)",
    re.IGNORECASE,
)
_GLOBAL_CORRECTION_RE = re.compile(
    r"(?:globally|across\s+all|for\s+every\s+project|全体で|いつでも|"
    r"どのプロジェクトでも|どの案件でも|すべての案件で)",
    re.IGNORECASE,
)
_TASK_CORRECTION_RE = re.compile(
    r"(?:このタスクだけ|この作業だけ|for\s+this\s+task)", re.IGNORECASE
)
_SESSION_CORRECTION_RE = re.compile(
    r"(?:今回だけ|この会話だけ|このやり取りだけ|this\s+time\s+only|"
    r"for\s+this\s+session)",
    re.IGNORECASE,
)
_CORRECTION_MARKER_RE = re.compile(
    r"(?:正しくは|ではなく|じゃなくて|違います?[。、, ]*|違う[。、, ]*|訂正[:：]?|"
    r"actually[, ]*|correction[:：]?)",
    re.IGNORECASE,
)
_EXTERNAL_PRINCIPAL_RE = re.compile(
    r"^(?P<provider>[a-z][a-z0-9._-]{0,31}):"
    r"(?P<tenant>[^:\s]{1,64}):(?P<subject>[^:\s]{1,64})$",
    re.IGNORECASE,
)


class ScopedMemoryError(RuntimeError):
    status_code = 400


class ScopedMemoryNotFound(ScopedMemoryError):
    status_code = 404


class ScopedMemoryPermissionDenied(ScopedMemoryError):
    status_code = 403


class ScopedMemoryConflict(ScopedMemoryError):
    status_code = 409


class ScopedMemoryValidationError(ScopedMemoryError):
    status_code = 422


@dataclass(frozen=True)
class MemoryScope:
    scope_type: str
    scope_id: str
    user_id: str
    project_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "user_id": self.user_id,
            "project_id": str(self.project_id) if self.project_id else None,
            "task_id": str(self.task_id) if self.task_id else None,
            "session_id": str(self.session_id) if self.session_id else None,
        }


def _uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _lock_scoped_memory_actor(session: Any, actor_id: str) -> None:
    """Serialize actor consent reads/writes across all PostgreSQL workers.

    A missing ``ScopedMemoryPrincipal`` row is a valid enabled-by-default
    state, so a row lock alone cannot close the create-vs-final-write race.
    The transaction advisory lock is keyed by the canonical actor identity and
    therefore serializes both existing-row updates and first-time materialization.
    Non-PostgreSQL test/runtime adapters deliberately retain their existing
    process-local behavior and do not receive PostgreSQL-only SQL.
    """

    get_bind = getattr(session, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    dialect_name = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect_name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"aoi-scoped-memory-actor:{actor_id}"},
    )


async def _lock_scoped_memory_project(session: Any, project_id: uuid.UUID) -> None:
    """Acquire the shared project invariant lock before Project row locks.

    Task/Docs writers use ``lock_task_project_ids`` with this exact namespace.
    Sharing that transaction advisory lock prevents the memory final gate from
    entering the Project -> User graph while a task writer is in the inverse
    User wait, without making SQLite/fake adapters execute PostgreSQL SQL.
    """

    get_bind = getattr(session, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    dialect_name = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect_name != "postgresql":
        return
    await lock_task_project_ids(session, (project_id,))


def _same_uuid(left: Any, right: Any) -> bool:
    """Compare UUID-bearing values by canonical value, never raw casing."""

    left_uuid = _uuid(left)
    right_uuid = _uuid(right)
    return (
        left_uuid is not None
        and right_uuid is not None
        and left_uuid == right_uuid
    )


def _normalized_chat_text(value: Any) -> str:
    """Normalize persisted/user payload text without erasing its meaning."""

    return " ".join(
        unicodedata.normalize("NFKC", str(value or "").replace("\r\n", "\n"))
        .strip()
        .split()
    )


def _same_actor_id(left: Any, right: Any) -> bool:
    """Compare UUID or namespaced actor ids without trusting casing."""

    if _same_uuid(left, right):
        return True
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def _canonical_actor_id(value: Any) -> str:
    """Return the stable owner key used by every Scoped Memory query.

    UUID-backed AoiTalk users retain their normal textual UUID identity.  An
    external principal is an opaque, namespaced key and is deliberately not
    resolved to (or materialized as) a ``users`` row.  Discord keys are
    canonicalized to a lowercase provider prefix while preserving the
    guild/user components exactly as supplied by the integration.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ScopedMemoryValidationError("principal is required")
    parsed = _uuid(raw)
    if parsed is not None:
        return str(parsed)
    if len(raw) > 100 or "\x00" in raw:
        raise ScopedMemoryValidationError("invalid principal")
    match = _EXTERNAL_PRINCIPAL_RE.fullmatch(raw)
    if match is not None:
        provider = match.group("provider").casefold()
        tenant = match.group("tenant")
        subject = match.group("subject")
        # The canonical Discord form is intentionally three-part and does not
        # permit a caller to collapse guild and user into one shared bucket.
        if provider == "discord":
            return f"discord:{tenant}:{subject}"
        return f"{provider}:{tenant}:{subject}"
    # Keep legacy opaque owners working during rollout.  New integrations
    # should use the namespaced form above; the string owner column remains
    # bounded and all ACL checks are exact-key comparisons.
    return raw


def _is_discord_principal(value: Any) -> bool:
    """Return whether an owner key is a canonical Discord external principal."""
    try:
        return _canonical_actor_id(value).casefold().startswith("discord:")
    except ScopedMemoryError:
        return False


def _normalized_text(value: Any) -> str:
    text = " ".join(str(value or "").strip().casefold().split())
    return re.sub(r"[^0-9a-zぁ-んァ-ン一-龥]+", "", text)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def project_memory_semantic_identity(value: Any) -> str:
    """Return the deterministic identity used to reconcile Project Memory.

    Project Memory has two independent writers today: the fast conversation
    extractor and Project Steward.  Their prose and memory types are allowed
    to differ, so matching on content (or using a fuzzy similarity threshold)
    would either miss an exact concept or merge unrelated concepts.  Both
    writers instead persist this opaque, versioned identity derived from the
    caller's semantic key.  NFKC plus :func:`_normalized_text` keeps the
    identity stable across Unicode width/case/whitespace variants while the
    version prefix leaves room for a future canonicalization change.

    Empty values return an empty string.  This lets reconciliation require a
    meaningful identity instead of accidentally treating the hash of an empty
    value as a durable concept.
    """

    raw = str(value or "").strip()
    if not raw:
        return ""
    # Persisted identities are opaque/versioned values.  Re-normalize only
    # their casing so callers can safely pass values read from JSON.
    lowered = raw.casefold()
    if re.fullmatch(r"pmem:v1:[0-9a-fA-F]{40}", lowered):
        return lowered
    normalized = _normalized_text(unicodedata.normalize("NFKC", raw))
    if not normalized:
        # ``_normalized_text`` intentionally keeps a conservative allowlist.
        # For scripts outside that list retain a canonical NFKC/casefold form
        # rather than collapsing every such semantic value to the empty key.
        normalized = unicodedata.normalize("NFKC", raw).casefold().strip()
    if not normalized:
        return ""
    return f"pmem:v1:{_digest(normalized)[:40]}"


_PROJECT_MEMORY_EVIDENCE_PREFIXES = {
    "chat": "chat",
    "conversation": "chat",
    "message": "chat",
    "task": "task",
    "tasks": "task",
    "doc": "docs",
    "docs": "docs",
    "document": "docs",
    "documents": "docs",
}

# Only these evidence identities can be resolved to a durable row and
# revalidated at the Project storage boundary.  Other source-qualified values
# (for example ``job:<id>`` from an integration) remain useful opaque
# provenance, but they are not sufficient authorization for an automatic
# Project Memory mutation on their own.
_DB_VERIFIABLE_PROJECT_EVIDENCE_PREFIXES = frozenset(
    {
        "chat",
        "task",
        "task_activity",
        "docs_node",
        "docs_revision",
        "project_knowledge_ref",
    }
)

_AUTOMATIC_MEMORY_SOURCE_PREFIXES = (
    "dreaming",
    "project_steward",
    "history_consolidation",
    "learning_capture",
)


def _evidence_source_hint(value: Any) -> str:
    """Return a canonical evidence source hint for a JSON evidence value."""

    if not isinstance(value, Mapping):
        return ""
    for key in ("source", "type", "kind", "evidence_type"):
        candidate = str(value.get(key) or "").strip().casefold()
        if not candidate:
            continue
        # Collector rows use values such as ``chat``/``tasks`` while the
        # fast-path uses ``conversation``.  Only map known source names;
        # unknown providers must remain opaque rather than being guessed.
        candidate = candidate.split(":", 1)[0]
        mapped = _PROJECT_MEMORY_EVIDENCE_PREFIXES.get(candidate)
        if mapped:
            return mapped
    return ""


def canonical_project_memory_evidence_id(
    value: Any,
    *,
    source: str | None = None,
    _allow_bare_uuid: bool = False,
) -> str | None:
    """Canonicalize one Project evidence identity without fuzzy matching.

    Conversation evidence is represented as ``chat:<ConversationMessage
    UUID>``.  Task and Docs evidence already carry source-qualified IDs and
    are preserved (apart from canonicalizing known source aliases).  Mapping
    inputs may be collector/evidence-ref JSON objects; only explicit identity
    fields are considered.  Prose, timestamps, and arbitrary IDs are never
    treated as evidence identities.
    """

    source_hint = str(source or "").strip().casefold()
    if source_hint:
        source_hint = _PROJECT_MEMORY_EVIDENCE_PREFIXES.get(
            source_hint.split(":", 1)[0], source_hint.split(":", 1)[0]
        )

    if isinstance(value, Mapping):
        source_hint = source_hint or _evidence_source_hint(value)
        # ``evidence_identity`` is the strongest explicit field and is used
        # by the fast path when a provider exposes a stable source ID.
        for key in (
            "evidence_identity",
            "evidence_id",
            "message_id",
            "source_ref",
        ):
            candidate = value.get(key)
            if candidate in (None, ""):
                continue
            result = canonical_project_memory_evidence_id(
                candidate,
                source=source_hint,
                # A bare ConversationMessage UUID is authoritative only when
                # it arrived in the explicit ``message_id`` field.  Bare UUIDs
                # in generic evidence_id/source_ref fields are rejected.
                _allow_bare_uuid=(key == "message_id"),
            )
            if result:
                return result
        return None

    if value in (None, ""):
        return None
    raw = unicodedata.normalize("NFKC", str(value)).strip()
    # Evidence identities are persisted in bounded JSON fields.  Reject
    # oversized/embedded-NUL values before any source-prefix handling so a
    # caller cannot smuggle unbounded provenance into reconciliation payloads.
    if not raw or len(raw) > 255 or "\x00" in raw:
        return None

    # Already source-qualified IDs are preserved.  The known aliases are
    # normalized (conversation -> chat, doc -> docs).  ``chat:`` is special:
    # only a server-issued ConversationMessage UUID is authoritative.  Other
    # source-qualified IDs (task/docs/job and integration-specific sources) are
    # opaque but stable and are intentionally preserved verbatim.
    if ":" in raw:
        prefix, suffix = raw.split(":", 1)
        prefix_key = prefix.strip().casefold()
        mapped = _PROJECT_MEMORY_EVIDENCE_PREFIXES.get(prefix_key)
        if mapped:
            suffix = suffix.strip()
            if not suffix:
                return None
            if mapped == "chat":
                try:
                    suffix = str(uuid.UUID(suffix))
                except (TypeError, ValueError, AttributeError):
                    return None
            return f"{mapped}:{suffix}"
        # Keep source-qualified integration identities (for example
        # ``job:<ScopedMemoryJob UUID>``) stable while rejecting malformed
        # prefixes/empty suffixes.  This branch deliberately does not infer a
        # source for an unqualified value.
        if (
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", prefix.strip())
            and suffix.strip()
        ):
            return f"{prefix.strip().casefold()}:{suffix.strip()}"
        return None

    # A bare UUID is accepted only from an explicit ``message_id`` mapping
    # field.  Direct callers must pass a source-qualified chat identity.
    if _allow_bare_uuid:
        try:
            parsed_uuid = uuid.UUID(raw)
        except (TypeError, ValueError, AttributeError):
            parsed_uuid = None
        if parsed_uuid is not None:
            return f"chat:{parsed_uuid}"
    return None


# Compatibility aliases keep the helper discoverable to integrations that
# describe this value as an "identity" rather than an "ID".
project_memory_evidence_identity = canonical_project_memory_evidence_id
canonical_evidence_identity = canonical_project_memory_evidence_id
# Internal spelling used by a few service adapters; keep it as a strict alias
# rather than duplicating canonicalization logic.
_canonical_project_evidence_id = canonical_project_memory_evidence_id


def project_memory_evidence_ids(
    memory: ContextMemory | Mapping[str, Any] | None,
) -> set[str]:
    """Extract canonical explicit evidence identities from a memory JSON row.

    The helper intentionally reads only existing ``ContextMemory`` JSON
    fields.  It never performs a database lookup and never mutates the row.
    Both fast-path and Steward rows have historically used slightly different
    provenance shapes, so ``evidence_refs`` and structured-data identity
    fields are accepted.  Unknown/non-identity values are ignored.
    """

    if memory is None:
        return set()
    if isinstance(memory, Mapping):
        evidence_refs = memory.get("evidence_refs")
        structured_data = memory.get("structured_data")
        projection_metadata = memory.get("projection_metadata")
    else:
        evidence_refs = getattr(memory, "evidence_refs", None)
        structured_data = getattr(memory, "structured_data", None)
        projection_metadata = getattr(memory, "projection_metadata", None)

    identities: set[str] = set()

    def add(
        value: Any,
        *,
        source: str | None = None,
        allow_bare_uuid: bool = False,
    ) -> None:
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                add(item, source=source, allow_bare_uuid=allow_bare_uuid)
            return
        identity = canonical_project_memory_evidence_id(
            value,
            source=source,
            _allow_bare_uuid=allow_bare_uuid,
        )
        if identity:
            identities.add(identity)

    if isinstance(evidence_refs, (list, tuple, set, frozenset)):
        for ref in evidence_refs:
            if isinstance(ref, Mapping):
                source_hint = _evidence_source_hint(ref) or None
                # Restrict extraction to explicit fields.  In particular,
                # ``source_ref=conversation_session:<uuid>`` is a session
                # provenance pointer, not the ConversationMessage evidence.
                for key in (
                    "evidence_identity",
                    "evidence_id",
                    "message_id",
                ):
                    if ref.get(key) not in (None, ""):
                        add(
                            ref.get(key),
                            source=source_hint,
                            allow_bare_uuid=key == "message_id",
                        )
                source_ref = ref.get("source_ref")
                if isinstance(source_ref, str) and ":" in source_ref:
                    prefix = source_ref.split(":", 1)[0].strip().casefold()
                    if prefix in _PROJECT_MEMORY_EVIDENCE_PREFIXES:
                        add(source_ref, source=source_hint)
            else:
                # Scalar refs are accepted only when already source-qualified
                # or UUID-shaped; arbitrary prose remains ignored.
                add(ref)

    for payload in (structured_data, projection_metadata):
        if not isinstance(payload, Mapping):
            continue
        source_hint = _evidence_source_hint(payload) or None
        for key in ("evidence_identity", "evidence_ids"):
            if payload.get(key) not in (None, ""):
                add(payload.get(key), source=source_hint)
        # Some fast-path payloads nest provenance under source_metadata.
        metadata = payload.get("source_metadata")
        if isinstance(metadata, Mapping):
            nested_source = _evidence_source_hint(metadata) or source_hint
            for key in ("evidence_identity", "evidence_id", "message_id"):
                if metadata.get(key) not in (None, ""):
                    add(
                        metadata.get(key),
                        source=nested_source,
                        allow_bare_uuid=key == "message_id",
                    )

    return identities


def _canonical_persisted_project_memory_evidence_refs(
    evidence_refs: Iterable[Any] | None = None,
    *,
    structured_data: Mapping[str, Any] | None = None,
    projection_metadata: Mapping[str, Any] | None = None,
    source_message_id: Any = None,
) -> list[dict[str, Any]]:
    """Materialize every explicit evidence identity at the storage boundary.

    ``project_memory_evidence_ids`` accepts both structured provenance and
    scalar source-qualified refs so the automatic Project gate can validate
    them.  Persisting only mapping-shaped ``evidence_refs`` would let a
    scalar (or a payload-level ``evidence_ids`` value) authorize a write and
    then disappear from the durable citation list.  Keep caller mappings
    lossless, convert recognized scalar refs to canonical records, and add
    any explicit identities found in the structured payloads exactly once.
    Unknown/prose values remain omitted and are therefore unable to create a
    synthetic citation for an automatic Project write.
    """

    refs: list[dict[str, Any]] = []
    persisted_ids: set[str] = set()
    raw_refs = (
        [evidence_refs]
        if isinstance(evidence_refs, (Mapping, str, bytes))
        else (evidence_refs or ())
    )

    for raw in raw_refs:
        if isinstance(raw, Mapping):
            ref = dict(raw)
            refs.append(ref)
            persisted_ids.update(project_memory_evidence_ids({"evidence_refs": [ref]}))
            continue
        identity = canonical_project_memory_evidence_id(raw)
        if not identity:
            continue
        refs.append(
            {
                "type": identity.split(":", 1)[0],
                "evidence_id": identity,
            }
        )
        persisted_ids.add(identity)

    payload_ids = project_memory_evidence_ids(
        {
            "structured_data": structured_data,
            "projection_metadata": projection_metadata,
        }
    )
    source_message_uuid = _uuid(source_message_id)
    if source_message_uuid is not None:
        payload_ids.add(f"chat:{source_message_uuid}")

    for identity in sorted(payload_ids - persisted_ids):
        refs.append(
            {
                "type": identity.split(":", 1)[0],
                "evidence_id": identity,
            }
        )
    return refs


def _project_memory_db_verifiable_evidence_ids(
    memory: ContextMemory | Mapping[str, Any] | None,
) -> set[str]:
    """Return only evidence identities backed by a database row.

    ``project_memory_evidence_ids`` deliberately preserves opaque,
    source-qualified integration identities for lineage and reconciliation.
    Automatic Project writes need a stricter subset: an identity must name a
    server-owned chat, Task, or Docs row that the writer can lock and
    revalidate in the same transaction.
    """

    return {
        identity
        for identity in project_memory_evidence_ids(memory)
        if identity.split(":", 1)[0].casefold()
        in _DB_VERIFIABLE_PROJECT_EVIDENCE_PREFIXES
    }


def _is_automatic_memory_source(source_type: Any) -> bool:
    """Return whether a source is an automatic Project-capable writer."""

    source_key = str(source_type or "").strip().casefold()
    return source_key.startswith(_AUTOMATIC_MEMORY_SOURCE_PREFIXES)


def _project_memory_semantic_identities(
    memory: ContextMemory | Mapping[str, Any] | None,
) -> set[str]:
    """Return deterministic semantic identities persisted in a memory row."""

    if memory is None:
        return set()
    if isinstance(memory, Mapping):
        structured_data = memory.get("structured_data")
        projection_metadata = memory.get("projection_metadata")
        content = memory.get("content")
    else:
        structured_data = getattr(memory, "structured_data", None)
        projection_metadata = getattr(memory, "projection_metadata", None)
        content = getattr(memory, "content", None)

    identities: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                add(item)
            return
        if value in (None, ""):
            return
        text_value = str(value).strip()
        if not text_value:
            return
        # A persisted versioned identity is already canonical.  Accept only
        # the exact vocabulary emitted by this helper; arbitrary provider
        # values are interpreted as semantic keys and canonicalized below.
        canonical = text_value.casefold()
        if canonical.startswith("pmem:v1:"):
            suffix = canonical[len("pmem:v1:") :]
            if re.fullmatch(r"[0-9a-f]{40}", suffix):
                identities.add(canonical)
                return
        identities.add(project_memory_semantic_identity(text_value))

    for payload in (structured_data, projection_metadata):
        if not isinstance(payload, Mapping):
            continue
        for key in ("semantic_identity", "semantic_identities"):
            if payload.get(key) not in (None, ""):
                add(payload.get(key))
        # Project Steward's stable identity is its semantic_key.  Derive the
        # versioned form for legacy rows that predate semantic_identity.
        if payload.get("semantic_key") not in (None, ""):
            add(payload.get("semantic_key"))

    # Fast-path Project Memory historically had no semantic_key field and
    # therefore persists the canonical identity of its durable content.  Keep
    # that identity as a first-class matching dimension; exact callers still
    # have to provide both evidence and semantic identities, so this is not a
    # fuzzy content fallback.
    if content not in (None, ""):
        add(content)

    return identities


def _dedupe_key(content: str, memory_type: str, explicit: str | None = None) -> str:
    if explicit:
        return str(explicit).strip()[:128]
    return _digest(f"{memory_type.casefold()}:{_normalized_text(content)}")


@asynccontextmanager
async def _local_dedupe_lock(material: str):
    """Serialize non-PostgreSQL/test writers without leaking per-key locks."""
    with _DEDUPE_LOCKS_GUARD:
        lock, references = _DEDUPE_LOCKS.get(material, (asyncio.Lock(), 0))
        _DEDUPE_LOCKS[material] = (lock, references + 1)
    acquired = False
    try:
        await lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            lock.release()
        with _DEDUPE_LOCKS_GUARD:
            current_lock, references = _DEDUPE_LOCKS.get(material, (lock, 1))
            if current_lock is lock and references <= 1:
                _DEDUPE_LOCKS.pop(material, None)
            elif current_lock is lock:
                _DEDUPE_LOCKS[material] = (lock, references - 1)


def classify_sensitivity(content: str) -> tuple[str, str | None]:
    if _SECRET_RE.search(content):
        return "secret", "secret-like material is not allowed in memory"
    if _SENSITIVE_RE.search(content):
        return "sensitive", None
    return "normal", None


def classify_sensitivity_fields(*values: Any) -> tuple[str, str | None]:
    """Classify every textual leaf in provider-controlled memory fields.

    Automatic extraction must not hide a secret in a title, semantic key,
    evidence span, or nested JSON value while presenting harmless content.
    Mapping keys are checked as well because model output can encode a secret
    field name with an otherwise innocuous value.
    """

    def visit(value: Any) -> tuple[str, str | None]:
        if isinstance(value, str):
            return classify_sensitivity(value)
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if isinstance(key, str):
                    sensitivity, reason = classify_sensitivity(key)
                    if sensitivity != "normal" or reason:
                        return sensitivity, reason
                sensitivity, reason = visit(nested)
                if sensitivity != "normal" or reason:
                    return sensitivity, reason
            return "normal", None
        if isinstance(value, (list, tuple, set, frozenset)):
            for nested in value:
                sensitivity, reason = visit(nested)
                if sensitivity != "normal" or reason:
                    return sensitivity, reason
        return "normal", None

    for value in values:
        sensitivity, reason = visit(value)
        if sensitivity != "normal" or reason:
            return sensitivity, reason
    return "normal", None


def _scope(
    *,
    actor_id: str,
    scope_type: str,
    scope_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
) -> MemoryScope:
    actor_id = _canonical_actor_id(actor_id)
    normalized_type = str(scope_type or "user").strip().casefold()
    if normalized_type not in VALID_SCOPES:
        raise ScopedMemoryValidationError(f"unsupported memory scope: {scope_type}")
    project_uuid = _uuid(project_id)
    task_uuid = _uuid(task_id)
    session_uuid = _uuid(session_id)
    resolved_scope_id = str(scope_id or "").strip()
    if normalized_type == "global":
        resolved_scope_id = resolved_scope_id or "global"
    elif normalized_type == "user":
        resolved_scope_id = resolved_scope_id or str(actor_id)
    elif normalized_type == "project":
        if not project_uuid:
            project_uuid = _uuid(resolved_scope_id)
        if not project_uuid:
            raise ScopedMemoryValidationError("project scope requires project_id")
        resolved_scope_id = str(project_uuid)
    elif normalized_type == "task":
        if not task_uuid:
            task_uuid = _uuid(resolved_scope_id)
        if not task_uuid:
            raise ScopedMemoryValidationError("task scope requires task_id")
        resolved_scope_id = str(task_uuid)
    elif normalized_type == "session":
        if not session_uuid:
            session_uuid = _uuid(resolved_scope_id)
        if not session_uuid:
            raise ScopedMemoryValidationError("session scope requires session_id")
        resolved_scope_id = str(session_uuid)
    return MemoryScope(
        scope_type=normalized_type,
        scope_id=resolved_scope_id,
        user_id=str(actor_id),
        project_id=project_uuid,
        task_id=task_uuid,
        session_id=session_uuid,
    )


def _turn_context(explicit: dict[str, Any] | None, tool_call_id: str | None) -> dict[str, Any]:
    if explicit is not None:
        context = dict(explicit)
    else:
        try:
            from .turn_context import get_turn_context

            current = get_turn_context()
            context = {
                "user_id": current.user_id,
                "project_id": current.project_id,
                "session_id": current.session_id,
                "message_id": current.message_id,
                "client_message_id": current.client_message_id,
                "tool_call_id": current.tool_call_id,
            }
        except Exception:
            context = {}
    if tool_call_id:
        context["tool_call_id"] = str(tool_call_id)
    return {key: value for key, value in context.items() if value not in (None, "")}


def _audit_snapshot(memory: ContextMemory | dict[str, Any] | None) -> dict[str, Any]:
    if memory is None:
        return {}
    if isinstance(memory, dict):
        return dict(memory)
    content = str(memory.content or "")
    return {
        "id": str(memory.id),
        "scope_type": memory.scope_type,
        "scope_id": memory.scope_id,
        "memory_type": memory.memory_type,
        "content_sha256": _digest(content),
        "status": memory.status,
        "version": memory.version,
        "dedupe_key": memory.dedupe_key,
        "supersedes_id": str(memory.supersedes_id) if memory.supersedes_id else None,
    }


def _coerce_temporal_timestamp(value: Any) -> datetime | None:
    """Normalize a persisted evidence timestamp to a naive UTC datetime.

    Conversation timestamps are historically a mixture of naive ISO strings,
    timezone-aware ISO strings, and ``datetime`` values returned by adapters.
    Comparing them without normalization can either raise or let an older
    backfill overwrite a newer incremental memory.
    """
    if value in (None, ""):
        return None
    candidate = value if isinstance(value, datetime) else None
    if candidate is None:
        try:
            candidate = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if candidate.tzinfo is not None:
        from datetime import timezone

        candidate = candidate.astimezone(timezone.utc).replace(tzinfo=None)
    return candidate


def _evidence_max_created_at(
    evidence_refs: Any = None,
    structured_data: Any = None,
    projection_metadata: Any = None,
) -> datetime | None:
    """Extract the newest source timestamp from a memory provenance payload."""
    candidates: list[datetime] = []
    refs = evidence_refs if isinstance(evidence_refs, (list, tuple)) else []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        for key in (
            "evidence_max_created_at",
            "message_created_at",
            "created_at",
            "timestamp",
            "occurred_at",
            "source_at",
            "source_updated_at",
        ):
            parsed = _coerce_temporal_timestamp(ref.get(key))
            if parsed is not None:
                candidates.append(parsed)
    payloads = [
        structured_data if isinstance(structured_data, dict) else {},
        projection_metadata if isinstance(projection_metadata, dict) else {},
    ]
    for payload in payloads:
        dreaming = payload.get("dreaming")
        if isinstance(dreaming, dict):
            for key in ("evidence_max_created_at", "max_created_at", "created_at"):
                parsed = _coerce_temporal_timestamp(dreaming.get(key))
                if parsed is not None:
                    candidates.append(parsed)
        for key in (
            "evidence_max_created_at",
            "max_created_at",
            "source_updated_at",
            "source_at",
            "created_at",
        ):
            parsed = _coerce_temporal_timestamp(payload.get(key))
            if parsed is not None:
                candidates.append(parsed)
    return max(candidates) if candidates else None


class ScopedMemoryService:
    """Single mutation and retrieval boundary for all memory scopes."""

    def __init__(
        self,
        session_factory: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._session_factory = session_factory or get_db_session

    async def _new_session(self):
        return await self._session_factory()
    @staticmethod
    def _overview_active_project_id(
        memory: ContextMemory | None,
    ) -> uuid.UUID | None:
        """Return the Project whose active Overview input is this memory."""

        if memory is None:
            return None
        if str(getattr(memory, "scope_type", "") or "") != "project":
            return None
        if str(getattr(memory, "status", "") or "") != "active":
            return None
        return _uuid(
            getattr(memory, "project_id", None)
            or getattr(memory, "scope_id", None)
        )

    async def _enqueue_project_overview_refresh_best_effort(
        self,
        *,
        project_ids: Iterable[Any],
        actor_id: str,
        reason: str,
    ) -> None:
        """Queue Overview refreshes only after the Memory transaction committed."""

        normalized: list[uuid.UUID] = []
        seen: set[uuid.UUID] = set()
        for raw_project_id in project_ids:
            project_id = _uuid(raw_project_id)
            if project_id is None or project_id in seen:
                continue
            seen.add(project_id)
            normalized.append(project_id)
        if not normalized:
            return

        try:
            from .project_overview_service import enqueue_project_overview_refresh
        except Exception:
            logger.warning(
                "Project Overview refresh enqueue import failed",
                exc_info=True,
            )
            return

        clean_reason = (
            str(reason or "scoped_memory_changed")
            .replace("\x00", "")
            .strip()[:128]
            or "scoped_memory_changed"
        )
        for project_id in normalized:
            try:
                await enqueue_project_overview_refresh(
                    project_id,
                    str(actor_id),
                    clean_reason,
                    session_factory=self._session_factory,
                )
            except asyncio.CancelledError:
                # The Memory commit already succeeded.  Shutdown/cancellation
                # of this best-effort projection must not turn that durable
                # mutation into a failed caller-visible operation.
                logger.debug(
                    "Project Overview refresh enqueue cancelled after Memory commit: %s",
                    project_id,
                )
            except Exception:
                logger.warning(
                    "Project Overview refresh enqueue failed after Memory commit: %s",
                    project_id,
                    exc_info=True,
                )

    @asynccontextmanager
    async def _serialized_dedupe_session(self, material: str):
        async with _local_dedupe_lock(material):
            async with await self._new_session() as session:
                bind = session.get_bind()
                if bind.dialect.name == "postgresql":
                    advisory_key = int.from_bytes(
                        hashlib.sha256(material.encode("utf-8")).digest()[:8],
                        byteorder="big",
                        signed=True,
                    )
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"),
                        {"key": advisory_key},
                    )
                yield session

    @asynccontextmanager
    async def _serialized_project_reconciliation_guard(
        self,
        *,
        project_id: uuid.UUID,
        evidence_ids: Iterable[str],
    ):
        """Serialize exact Project-Memory reconciliation through its write.

        Fast conversation capture and Project Steward use different
        ``dedupe_key``/namespace contracts, so their ordinary upsert locks do
        not protect the cross-path probe.  This guard is keyed by Project and
        each canonical evidence identity (not by semantic identity) and is
        held until the caller's canonical writer has committed.  PostgreSQL
        uses transaction-scoped advisory locks for cross-process workers;
        non-PostgreSQL adapters retain the existing process-local asyncio
        serialization used by the test/runtime compatibility paths.
        """

        canonical_evidence = sorted(
            {
                identity
                for raw in evidence_ids
                for identity in (canonical_project_memory_evidence_id(raw),)
                if identity
            }
        )
        if not canonical_evidence:
            yield
            return

        materials = [
            f"project-memory-reconcile:v1:{project_id}:{evidence_id}"
            for evidence_id in canonical_evidence
        ]
        # Sorted acquisition avoids deadlocks when a future caller cites more
        # than one evidence item.  The lock registry is module-global, so
        # separate ScopedMemoryService instances in one process coordinate.
        async with AsyncExitStack() as stack:
            for material in materials:
                await stack.enter_async_context(_local_dedupe_lock(material))

            # Keep one transaction open while the caller performs its normal
            # upsert transaction.  PostgreSQL advisory locks remain held until
            # this outer session exits, after the writer has committed.
            async with await self._new_session() as lock_session:
                get_bind = getattr(lock_session, "get_bind", None)
                bind = get_bind() if callable(get_bind) else None
                dialect_name = getattr(
                    getattr(bind, "dialect", None),
                    "name",
                    None,
                )
                if dialect_name == "postgresql":
                    for material in materials:
                        advisory_key = int.from_bytes(
                            hashlib.sha256(material.encode("utf-8")).digest()[:8],
                            byteorder="big",
                            signed=True,
                        )
                        await lock_session.execute(
                            text("SELECT pg_advisory_xact_lock(:key)"),
                            {"key": advisory_key},
                        )
                yield

    async def _require_project_permission(
        self,
        session,
        *,
        project_id: uuid.UUID,
        actor_id: str,
        write: bool,
    ) -> None:
        project = await session.get(Project, project_id)
        if project is None or project.deleted_at is not None:
            raise ScopedMemoryNotFound("project not found")
        actor_uuid = _uuid(actor_id)
        user = await session.get(User, actor_uuid) if actor_uuid else None
        if user is not None and user.role == "admin":
            return
        if actor_uuid and project.owner_id == actor_uuid:
            return
        if not actor_uuid:
            raise ScopedMemoryPermissionDenied("project access denied")
        member = (
            await session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == actor_uuid,
                )
            )
        ).scalar_one_or_none()
        permission = "write" if write else "read"
        permissions = normalize_project_member_permissions(
            getattr(member, "permissions", None) if member is not None else None
        )
        if permissions.get(permission) is not True:
            raise ScopedMemoryPermissionDenied("project access denied")

    async def _require_active_project_for_auto(
        self,
        session,
        *,
        actor_id: str,
        project_id: uuid.UUID,
    ) -> None:
        """Lock consent owners and require a live Project for auto writes.

        ``get_settings`` treats the Project toggle as an override of the
        actor's user/principal setting.  The final mutation gate must use the
        same effective value; otherwise a user-level opt-out with no Project
        override could be bypassed by calling the canonical writer directly.

        PostgreSQL uses two advisory locks before any row locks: the actor key
        closes the missing-external-principal create race, and the shared
        project key serializes this writer with Task/Docs project writers.  The
        row order is then Project -> User/Principal, matching those writers and
        avoiding a Project/User deadlock.
        """

        actor_key = _canonical_actor_id(actor_id)
        await _lock_scoped_memory_actor(session, actor_key)
        await _lock_scoped_memory_project(session, project_id)

        result = await session.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        project = result.scalar_one_or_none()
        if (
            project is None
            or getattr(project, "deleted_at", None) is not None
            or bool(getattr(project, "is_completed", False))
        ):
            raise ScopedMemoryPermissionDenied(
                "Project auto memory requires an active Project"
            )

        actor_uuid = _uuid(actor_key)
        actor_settings: Mapping[str, Any] = {}
        if actor_uuid is not None:
            user_result = await session.execute(
                select(User)
                .where(User.id == actor_uuid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            user = user_result.scalar_one_or_none()
            if user is None:
                raise ScopedMemoryPermissionDenied("authenticated user not found")
            raw_settings = getattr(user, "user_settings", None)
            if isinstance(raw_settings, Mapping):
                actor_settings = raw_settings
        else:
            principal_result = await session.execute(
                select(ScopedMemoryPrincipal)
                .where(ScopedMemoryPrincipal.principal_key == actor_key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            principal = principal_result.scalar_one_or_none()
            raw_settings = getattr(principal, "settings", None) if principal else None
            if isinstance(raw_settings, Mapping):
                actor_settings = raw_settings

        # The Project row is locked above, so an explicit consent change made
        # while an extractor/model was running cannot race the final write.
        # When the Project override is absent, the actor-level setting below
        # supplies the same historical enabled-by-default fallback as
        # ``get_settings``; once either key exists, only exact ``True`` is
        # consent.
        metadata = getattr(project, "project_metadata", None)
        if isinstance(metadata, Mapping) and "scoped_memory_auto_enabled" in metadata:
            if metadata.get("scoped_memory_auto_enabled") is not True:
                raise ScopedMemoryPermissionDenied(
                    "Project auto memory is disabled"
                )
            return

        scoped_settings = actor_settings.get("scoped_memory")
        raw_user_enabled = (
            scoped_settings.get("auto_enabled", True)
            if isinstance(scoped_settings, Mapping)
            else True
        )
        if raw_user_enabled is not True:
            raise ScopedMemoryPermissionDenied("Project auto memory is disabled")

    async def _require_scope_permission(
        self,
        session,
        *,
        scope: MemoryScope,
        actor_id: str,
        write: bool,
        expected_project_id: uuid.UUID | None = None,
    ) -> None:
        if scope.scope_type in {"global", "user"}:
            if scope.user_id != str(actor_id):
                raise ScopedMemoryPermissionDenied("cross-user memory access denied")
            return
        if scope.scope_type == "project":
            assert scope.project_id is not None
            await self._require_project_permission(
                session, project_id=scope.project_id, actor_id=actor_id, write=write
            )
            return
        if scope.scope_type == "task":
            task = await session.get(Task, scope.task_id)
            if task is None or task.deleted_at is not None:
                raise ScopedMemoryNotFound("task not found")
            if expected_project_id is not None and not _same_uuid(
                task.project_id, expected_project_id
            ):
                raise ScopedMemoryPermissionDenied("task is outside project scope")
            await self._require_project_permission(
                session, project_id=task.project_id, actor_id=actor_id, write=write
            )
            return
        conversation = await session.get(ConversationSession, scope.session_id)
        if conversation is None or conversation.deleted_at is not None:
            raise ScopedMemoryNotFound("session not found")
        if not _same_actor_id(conversation.user_id, actor_id):
            raise ScopedMemoryPermissionDenied("session access denied")
        if expected_project_id is not None and not _same_uuid(
            conversation.project_id, expected_project_id
        ):
            raise ScopedMemoryPermissionDenied("session is outside project scope")

    async def _require_project_chat_binding(
        self,
        session,
        *,
        actor_id: str,
        project_id: str | uuid.UUID,
        session_id: str | uuid.UUID | None,
        source_message_id: str | uuid.UUID | None = None,
        source_user_input: str | None = None,
    ) -> None:
        """Lock and validate the chat evidence behind an automatic Project write.

        This check deliberately runs in the same transaction as the final
        ``ContextMemory`` mutation.  A conversation reassignment or soft
        delete that races the extractor therefore either commits first and is
        rejected here, or waits for this writer to finish against the still
        valid Project binding.
        """

        project_uuid = _uuid(project_id)
        session_uuid = _uuid(session_id)
        if project_uuid is None or session_uuid is None:
            raise ScopedMemoryValidationError(
                "Project writes require canonical chat scope provenance"
            )
        # Project deletion/rebinding paths lock the Project before touching
        # ConversationSession.project_id.  Acquire that parent lock first so
        # an automatic Project write never takes Session -> Project while a
        # concurrent lifecycle mutation takes Project -> Session.
        project = (
            await session.execute(
                select(Project)
                .where(Project.id == project_uuid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            project is None
            or getattr(project, "deleted_at", None) is not None
            or bool(getattr(project, "is_completed", False))
        ):
            raise ScopedMemoryPermissionDenied("project is outside live scope")
        conversation = (
            await session.execute(
                select(ConversationSession)
                .where(ConversationSession.id == session_uuid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if conversation is None or conversation.deleted_at is not None:
            raise ScopedMemoryPermissionDenied("session is outside project scope")
        if not _same_actor_id(conversation.user_id, actor_id):
            raise ScopedMemoryPermissionDenied("session access denied")
        if not _same_uuid(conversation.project_id, project_uuid):
            raise ScopedMemoryPermissionDenied("session is outside project scope")

        if source_message_id not in (None, ""):
            message_uuid = _uuid(source_message_id)
            if message_uuid is None:
                raise ScopedMemoryValidationError(
                    "Project writes require a canonical source message id"
                )
            message = (
                await session.execute(
                    select(ConversationMessage)
                    .where(ConversationMessage.id == message_uuid)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if message is None or message.deleted_at is not None:
                raise ScopedMemoryPermissionDenied("source message is invalid")
            if not _same_uuid(message.session_id, session_uuid):
                raise ScopedMemoryPermissionDenied(
                    "source message is outside session scope"
                )
            if str(message.role or "").strip().casefold() != "user":
                raise ScopedMemoryPermissionDenied("source message is not user evidence")
            if not _same_actor_id(getattr(message, "sender_id", None), actor_id):
                raise ScopedMemoryPermissionDenied("source message is outside actor scope")
            if is_privacy_masking_source(message):
                raise ScopedMemoryPermissionDenied(
                    "privacy masking source cannot become Project Memory"
                )
            if source_user_input not in (None, "") and (
                not getattr(message, "content", None)
                or _normalized_chat_text(message.content)
                != _normalized_chat_text(source_user_input)
            ):
                raise ScopedMemoryPermissionDenied("source message content does not match")

    async def _require_project_chat_evidence(
        self,
        session,
        *,
        actor_id: str,
        project_id: str | uuid.UUID,
        evidence_refs: Iterable[dict[str, Any]] | None,
        structured_data: Mapping[str, Any] | None = None,
        projection_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Lock every canonical chat citation used by a Project write.

        Project Steward can cite several chat messages in one memory item,
        whereas the fast path has one source message.  The single-source
        binding above is therefore not enough to protect a multi-evidence
        Steward write: a cited message could be deleted or rebound after
        collection but before the final mutation.  Re-read and lock every
        ``chat:<ConversationMessage UUID>`` identity in this same transaction
        as the ContextMemory write.  Non-chat Task/Docs evidence is checked by
        the companion ``_require_project_nonchat_evidence`` helper below;
        unknown integration identities remain opaque.
        """

        probe = {
            "evidence_refs": list(evidence_refs or ()),
            "structured_data": structured_data,
            "projection_metadata": projection_metadata,
        }
        chat_ids = sorted(
            identity
            for identity in project_memory_evidence_ids(probe)
            if identity.casefold().startswith("chat:")
        )
        if not chat_ids:
            return
        project_uuid = _uuid(project_id)
        if project_uuid is None:
            raise ScopedMemoryValidationError(
                "Project writes require a canonical project id"
            )

        # Keep the lifecycle lock graph parent-first.  Project deletion and
        # chat rebinding lock Project before updating ConversationSession;
        # taking the same lock before any chat preview/row lock avoids a
        # Session -> Project versus Project -> Session deadlock.
        project = (
            await session.execute(
                select(Project)
                .where(Project.id == project_uuid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            project is None
            or getattr(project, "deleted_at", None) is not None
            or bool(getattr(project, "is_completed", False))
        ):
            raise ScopedMemoryPermissionDenied(
                "Project chat evidence is outside live Project scope"
            )

        # Preserve the normal session -> message lock order used by
        # _require_project_chat_binding.  A non-locking preview obtains each
        # message's session id; the authoritative rows are then locked in a
        # deterministic global order.  Sorting *all* sessions before any
        # message prevents two concurrent multi-evidence writes from taking
        # Session A -> Session B versus Session B -> Session A and deadlocking.
        message_sessions: list[tuple[uuid.UUID, uuid.UUID]] = []
        for evidence_id in chat_ids:
            try:
                message_uuid = uuid.UUID(evidence_id.split(":", 1)[1])
            except (TypeError, ValueError, AttributeError, IndexError) as exc:
                raise ScopedMemoryValidationError(
                    "Project evidence contains an invalid chat identity"
                ) from exc
            preview = await session.get(ConversationMessage, message_uuid)
            if preview is None:
                raise ScopedMemoryPermissionDenied(
                    "Project chat evidence is no longer available"
                )
            session_uuid = _uuid(getattr(preview, "session_id", None))
            if session_uuid is None:
                raise ScopedMemoryPermissionDenied(
                    "Project chat evidence has no canonical session"
                )
            message_sessions.append((message_uuid, session_uuid))

        for session_uuid in sorted({session_id for _, session_id in message_sessions}):
            conversation = (
                await session.execute(
                    select(ConversationSession)
                    .where(ConversationSession.id == session_uuid)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if conversation is None or conversation.deleted_at is not None:
                raise ScopedMemoryPermissionDenied(
                    "Project chat evidence session is unavailable"
                )
            if not _same_actor_id(
                getattr(conversation, "user_id", None), actor_id
            ):
                raise ScopedMemoryPermissionDenied(
                    "Project chat evidence is outside actor scope"
                )
            if not _same_uuid(
                getattr(conversation, "project_id", None), project_uuid
            ):
                raise ScopedMemoryPermissionDenied(
                    "Project chat evidence is outside Project scope"
                )

        for message_uuid, session_uuid in sorted(message_sessions):
            message = (
                await session.execute(
                    select(ConversationMessage)
                    .where(ConversationMessage.id == message_uuid)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if message is None or message.deleted_at is not None:
                raise ScopedMemoryPermissionDenied(
                    "Project chat evidence is no longer available"
                )
            if not _same_uuid(getattr(message, "session_id", None), session_uuid):
                raise ScopedMemoryPermissionDenied(
                    "Project chat evidence is outside session scope"
                )
            if str(getattr(message, "role", "") or "").strip().casefold() != "user":
                raise ScopedMemoryPermissionDenied(
                    "Project chat evidence is not user evidence"
                )
            if not _same_actor_id(getattr(message, "sender_id", None), actor_id):
                raise ScopedMemoryPermissionDenied(
                    "Project chat evidence is outside actor scope"
                )
            if is_privacy_masking_source(message):
                raise ScopedMemoryPermissionDenied(
                    "privacy masking source cannot become Project Memory"
                )

    async def _require_project_nonchat_evidence(
        self,
        session,
        *,
        actor_id: str,
        project_id: str | uuid.UUID,
        evidence_refs: Iterable[dict[str, Any]] | None,
        structured_data: Mapping[str, Any] | None = None,
        projection_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Revalidate Task/Docs evidence in the Project writer transaction.

        Project Steward collects Tasks and Docs before invoking the model, so
        a cited row can be moved, archived, deleted, or have its Docs ACL
        revoked while the model is composing a plan.  Chat evidence has its
        own binding helper; this companion locks every explicit Task/Docs
        identity and checks the current Project/ACL boundary immediately
        before the ContextMemory mutation.  Unknown integration identities
        remain opaque and are not guessed into a database row.
        """

        probe = {
            "evidence_refs": list(evidence_refs or ()),
            "structured_data": structured_data,
            "projection_metadata": projection_metadata,
        }
        identities = sorted(project_memory_evidence_ids(probe))
        task_ids: set[uuid.UUID] = set()
        task_activity_ids: set[uuid.UUID] = set()
        docs_node_ids: set[uuid.UUID] = set()
        docs_revision_ids: set[uuid.UUID] = set()
        project_ref_ids: set[uuid.UUID] = set()

        def parse_uuid_suffix(
            identity: str,
            prefix: str,
            *,
            at_suffix: bool = False,
        ) -> uuid.UUID | None:
            marker = f"{prefix}:"
            if not identity.casefold().startswith(marker):
                return None
            suffix = identity[len(marker) :]
            if at_suffix:
                suffix = suffix.split("@", 1)[0]
            try:
                return uuid.UUID(suffix)
            except (TypeError, ValueError, AttributeError):
                return None

        for identity in identities:
            lowered = identity.casefold()
            parsed: uuid.UUID | None
            if lowered.startswith("task_activity:"):
                parsed = parse_uuid_suffix(identity, "task_activity")
                if parsed is None:
                    raise ScopedMemoryValidationError(
                        "Project evidence contains an invalid task activity identity"
                    )
                task_activity_ids.add(parsed)
            elif lowered.startswith("task:"):
                parsed = parse_uuid_suffix(identity, "task", at_suffix=True)
                if parsed is None:
                    raise ScopedMemoryValidationError(
                        "Project evidence contains an invalid task identity"
                    )
                task_ids.add(parsed)
            elif lowered.startswith("docs_node:"):
                parsed = parse_uuid_suffix(identity, "docs_node")
                if parsed is None:
                    raise ScopedMemoryValidationError(
                        "Project evidence contains an invalid Docs node identity"
                    )
                docs_node_ids.add(parsed)
            elif lowered.startswith("docs_revision:"):
                parsed = parse_uuid_suffix(identity, "docs_revision")
                if parsed is None:
                    raise ScopedMemoryValidationError(
                        "Project evidence contains an invalid Docs revision identity"
                    )
                docs_revision_ids.add(parsed)
            elif lowered.startswith("project_knowledge_ref:"):
                parsed = parse_uuid_suffix(identity, "project_knowledge_ref")
                if parsed is None:
                    raise ScopedMemoryValidationError(
                        "Project evidence contains an invalid Project Docs reference identity"
                    )
                project_ref_ids.add(parsed)

        if not (
            task_ids
            or task_activity_ids
            or docs_node_ids
            or docs_revision_ids
            or project_ref_ids
        ):
            return

        project_uuid = _uuid(project_id)
        if project_uuid is None:
            raise ScopedMemoryValidationError(
                "Project writes require a canonical project id"
            )
        actor_uuid = _uuid(actor_id)
        if actor_uuid is None:
            raise ScopedMemoryPermissionDenied(
                "Task/Docs evidence requires an authenticated user"
            )
        execute = getattr(session, "execute", None)
        if not callable(execute):
            # A non-locking adapter cannot prove that a collected Task/Docs
            # row is still inside the requested Project.  Unlike opaque
            # legacy evidence, auto writers fail closed when this boundary is
            # unavailable.
            raise ScopedMemoryValidationError(
                "Task/Docs evidence validation requires a database session"
            )

        async def fetch(model: Any, identifier: uuid.UUID, *, lock: bool) -> Any:
            statement = select(model).where(model.id == identifier).execution_options(
                populate_existing=True
            )
            if lock:
                statement = statement.with_for_update()
            result = await execute(statement)
            scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
            if callable(scalar_one_or_none):
                return scalar_one_or_none()
            scalars = getattr(result, "scalars", None)
            if callable(scalars):
                values = scalars()
                first = getattr(values, "first", None)
                if callable(first):
                    return first()
            return None

        # Resolve dependent rows through an unlocked preview first.  The
        # preview only discovers each parent id; every row is fetched again
        # with ``FOR UPDATE`` below immediately before the write.  This keeps
        # the lock graph parent-first without trusting a stale collector
        # object for the final authorization decision.
        activity_by_id: dict[uuid.UUID, Any] = {}
        activity_task_id_by_id: dict[uuid.UUID, uuid.UUID] = {}
        for activity_id in sorted(task_activity_ids):
            activity = await fetch(TaskActivity, activity_id, lock=False)
            if activity is None:
                raise ScopedMemoryPermissionDenied(
                    "Project task activity evidence is no longer available"
                )
            activity_task_id = _uuid(getattr(activity, "task_id", None))
            if activity_task_id is None:
                raise ScopedMemoryPermissionDenied(
                    "Project task activity evidence has no canonical Task"
                )
            task_ids.add(activity_task_id)
            activity_by_id[activity_id] = activity
            activity_task_id_by_id[activity_id] = activity_task_id

        revision_node_ids: set[uuid.UUID] = set()
        revision_by_id: dict[uuid.UUID, Any] = {}
        revision_node_id_by_id: dict[uuid.UUID, uuid.UUID] = {}
        for revision_id in sorted(docs_revision_ids):
            revision = await fetch(KnowledgeRevision, revision_id, lock=False)
            if revision is None:
                raise ScopedMemoryPermissionDenied(
                    "Project Docs revision evidence is no longer available"
                )
            node_id = _uuid(getattr(revision, "node_id", None))
            if node_id is None:
                raise ScopedMemoryPermissionDenied(
                    "Project Docs revision evidence has no canonical node"
                )
            revision_node_ids.add(node_id)
            revision_by_id[revision_id] = revision
            revision_node_id_by_id[revision_id] = node_id

        ref_node_ids: set[uuid.UUID] = set()
        reference_by_id: dict[uuid.UUID, Any] = {}
        reference_node_id_by_id: dict[uuid.UUID, uuid.UUID] = {}
        for reference_id in sorted(project_ref_ids):
            reference = await fetch(ProjectKnowledgeRef, reference_id, lock=False)
            if reference is None:
                raise ScopedMemoryPermissionDenied(
                    "Project Docs reference evidence is no longer available"
                )
            node_id = _uuid(getattr(reference, "knowledge_node_id", None))
            if node_id is None:
                raise ScopedMemoryPermissionDenied(
                    "Project Docs reference evidence has no canonical node"
                )
            ref_node_ids.add(node_id)
            reference_by_id[reference_id] = reference
            reference_node_id_by_id[reference_id] = node_id

        # Project is the common parent for both Task and ProjectKnowledgeRef,
        # and the normal Task/Docs mutation paths lock it before their child
        # rows.  Acquire it first so this writer cannot hold a child lock while
        # waiting on a concurrent Project mutation.
        project = await fetch(Project, project_uuid, lock=True)
        if (
            project is None
            or getattr(project, "deleted_at", None) is not None
            or bool(getattr(project, "is_completed", False))
        ):
            raise ScopedMemoryPermissionDenied(
                "Project evidence is no longer in live Project scope"
            )

        # Task mutations use Task -> TaskActivity (delete/restore updates the
        # parent task rows before appending/deleting activity rows).  Lock all
        # parent tasks in UUID order before any dependent activity, then
        # re-check the activity's FK binding against the locked parent set.
        for task_id in sorted(task_ids):
            task = await fetch(Task, task_id, lock=True)
            if (
                task is None
                or not _same_uuid(getattr(task, "project_id", None), project_uuid)
                or getattr(task, "deleted_at", None) is not None
                or getattr(task, "archived_at", None) is not None
            ):
                raise ScopedMemoryPermissionDenied(
                    "Project Task evidence is no longer in live Project scope"
                )

        for activity_id in sorted(activity_by_id):
            activity = await fetch(TaskActivity, activity_id, lock=True)
            activity_task_id = _uuid(getattr(activity, "task_id", None)) if activity else None
            if (
                activity is None
                or activity_task_id is None
                or activity_task_id != activity_task_id_by_id[activity_id]
            ):
                raise ScopedMemoryPermissionDenied(
                    "Project task activity evidence is outside live Task scope"
                )

        # Docs writers lock Project -> KnowledgeNode and only then append a
        # KnowledgeRevision or update a ProjectKnowledgeRef.  Lock every
        # parent node in UUID order before either dependent type.  Revision
        # and reference rows are re-read under lock to close the preview race.
        direct_node_ids = docs_node_ids | revision_node_ids
        all_node_ids = direct_node_ids | ref_node_ids
        nodes: dict[uuid.UUID, Any] = {}
        for node_id in sorted(all_node_ids):
            node = await fetch(KnowledgeNode, node_id, lock=True)
            if node is None or getattr(node, "archived_at", None) is not None:
                raise ScopedMemoryPermissionDenied(
                    "Project Docs node evidence is no longer available"
                )
            nodes[node_id] = node

        for revision_id in sorted(revision_by_id):
            revision = await fetch(KnowledgeRevision, revision_id, lock=True)
            revision_node_id = (
                _uuid(getattr(revision, "node_id", None)) if revision else None
            )
            if (
                revision is None
                or revision_node_id is None
                or revision_node_id != revision_node_id_by_id[revision_id]
                or revision_node_id not in nodes
            ):
                raise ScopedMemoryPermissionDenied(
                    "Project Docs revision evidence is outside live Docs scope"
                )

        for reference_id in sorted(reference_by_id):
            reference = await fetch(ProjectKnowledgeRef, reference_id, lock=True)
            reference_node_id = (
                _uuid(getattr(reference, "knowledge_node_id", None))
                if reference
                else None
            )
            if (
                reference is None
                or not _same_uuid(getattr(reference, "project_id", None), project_uuid)
                or reference_node_id is None
                or reference_node_id != reference_node_id_by_id[reference_id]
                or reference_node_id not in nodes
            ):
                raise ScopedMemoryPermissionDenied(
                    "Project Docs reference evidence is outside Project scope"
                )

        # A ``docs_revision`` can be collected for a shared Personal Docs node
        # when this Project has an explicit ProjectKnowledgeRef to that node.
        # Such a node deliberately has ``project_id IS NULL``; treating every
        # revision node as a direct Project node would therefore reject valid
        # shared-reference evidence.  Build the set of locked, live references
        # that can authorize those revisions.  If the model cited only the
        # revision (and not the companion ref event), discover the current ref
        # under lock so the same Project+node binding is still required.
        direct_node_ids = set(docs_node_ids)
        shared_revision_node_ids: set[uuid.UUID] = set()
        for node_id in sorted(revision_node_ids):
            node = nodes[node_id]
            if _same_uuid(getattr(node, "project_id", None), project_uuid):
                direct_node_ids.add(node_id)
            else:
                shared_revision_node_ids.add(node_id)

        references_by_node: dict[uuid.UUID, list[Any]] = {}
        for reference in reference_by_id.values():
            reference_node_id = _uuid(getattr(reference, "knowledge_node_id", None))
            if (
                reference_node_id is not None
                and _same_uuid(getattr(reference, "project_id", None), project_uuid)
            ):
                references_by_node.setdefault(reference_node_id, []).append(reference)

        # ProjectKnowledgeRef is a dependent of the already-locked Project and
        # KnowledgeNode rows.  Acquire implicit refs in UUID order, matching
        # the explicit-ref lock order above and keeping the parent-first graph.
        for node_id in sorted(shared_revision_node_ids):
            if references_by_node.get(node_id):
                continue
            result = await execute(
                select(ProjectKnowledgeRef)
                .where(
                    ProjectKnowledgeRef.project_id == project_uuid,
                    ProjectKnowledgeRef.knowledge_node_id == node_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            scalars = getattr(result, "scalars", None)
            scalar_values = scalars() if callable(scalars) else None
            all_rows = getattr(scalar_values, "all", None)
            rows = list(all_rows() or []) if callable(all_rows) else []
            if not rows:
                raise ScopedMemoryPermissionDenied(
                    "Project Docs revision evidence has no live Project reference"
                )
            references_by_node[node_id] = rows

        # A shared Personal Docs node (whether reached through a
        # ``docs_revision`` or an explicit ``project_knowledge_ref``) is
        # authorized by the nearest explicit ``KnowledgeNodeShare`` on the
        # node or one of its parents.  Lock the complete ancestor closure
        # before evaluating the ACL predicate.  The frontend share PATCH/DELETE
        # paths update these rows directly, so a concurrent revoke either
        # commits before this lock (and is observed as unreadable) or waits
        # until after this transaction's memory mutation has committed.
        # Locking the ancestor KnowledgeNode rows as well prevents a concurrent
        # share INSERT (whose FK takes a key-share lock) from racing the ACL
        # decision.  Ordering by node/id keeps multi-node writers deterministic
        # and avoids share-row races.
        shared_acl_node_ids = {
            node_id
            for node_id in all_node_ids
            if getattr(nodes[node_id], "project_id", None) is None
        }
        if shared_acl_node_ids:
            acl_nodes = (
                select(
                    KnowledgeNode.id.label("node_id"),
                    KnowledgeNode.parent_id.label("parent_id"),
                    KnowledgeNode.docs_library_id.label("docs_library_id"),
                )
                .where(KnowledgeNode.id.in_(sorted(shared_acl_node_ids)))
                .cte("project_docs_acl_nodes", recursive=True)
            )
            parent_node = aliased(KnowledgeNode)
            acl_nodes = acl_nodes.union(
                select(
                    parent_node.id,
                    parent_node.parent_id,
                    parent_node.docs_library_id,
                )
                .join(acl_nodes, parent_node.id == acl_nodes.c.parent_id)
                .where(
                    parent_node.docs_library_id == acl_nodes.c.docs_library_id
                )
            )
            await execute(
                select(KnowledgeNode)
                .join(acl_nodes, KnowledgeNode.id == acl_nodes.c.node_id)
                .order_by(KnowledgeNode.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            await execute(
                select(KnowledgeNodeShare)
                .join(acl_nodes, KnowledgeNodeShare.node_id == acl_nodes.c.node_id)
                .where(KnowledgeNodeShare.user_id == actor_uuid)
                .order_by(KnowledgeNodeShare.node_id, KnowledgeNodeShare.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )

        for node_id in sorted(all_node_ids):
            node = nodes[node_id]
            if node_id in direct_node_ids and not _same_uuid(
                getattr(node, "project_id", None), project_uuid
            ):
                raise ScopedMemoryPermissionDenied(
                    "Project Docs node evidence is outside Project scope"
                )
            if node_id in direct_node_ids:
                # Direct ``docs_node``/Project-bound ``docs_revision`` evidence
                # is selected from KnowledgeNode.project_id == this Project by
                # the collector.  The locked Project-bound node plus the
                # writer's Project ACL check is the authoritative boundary; no
                # Personal share graph is involved for this path.
                continue
            if node_id in shared_revision_node_ids and not references_by_node.get(
                node_id
            ):
                # A shared revision is valid only through a current explicit
                # ProjectKnowledgeRef.  Do not silently treat a foreign or
                # unbound KnowledgeNode as Project evidence.
                raise ScopedMemoryPermissionDenied(
                    "Project Docs revision evidence is not Project-referenced"
                )
            docs_library_id = _uuid(getattr(node, "docs_library_id", None))
            if docs_library_id is None:
                raise ScopedMemoryPermissionDenied(
                    "Project Docs evidence has no canonical library"
                )
            readable = await execute(
                select(KnowledgeNode.id)
                .where(
                    KnowledgeNode.id == node_id,
                    KnowledgeNode.archived_at.is_(None),
                    docs_readable_node_predicate(
                        KnowledgeNode,
                        docs_library_id=docs_library_id,
                        user_id=actor_uuid,
                        required="read",
                    ),
                )
            )
            scalar = getattr(readable, "scalar_one_or_none", None)
            visible_id = scalar() if callable(scalar) else None
            if visible_id is None:
                raise ScopedMemoryPermissionDenied(
                    "Project Docs evidence is no longer readable"
                )

    @staticmethod
    def _scope_from_memory(memory: ContextMemory) -> MemoryScope:
        return MemoryScope(
            scope_type=memory.scope_type,
            scope_id=str(memory.scope_id or ""),
            user_id=str(memory.user_id or ""),
            project_id=memory.project_id,
            task_id=memory.task_id,
            session_id=memory.session_id,
        )

    @staticmethod
    def _result(
        memory: ContextMemory,
        *,
        operation: str,
        reason: str,
        replaced_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        payload = memory.to_dict()
        return {
            "success": True,
            "memory_id": str(memory.id),
            "scope": memory.scope_type,
            "scope_id": memory.scope_id,
            "operation": operation,
            "replaced_id": str(replaced_id) if replaced_id else None,
            "reason": reason,
            "memory": payload,
        }

    @staticmethod
    def _add_audit(
        session,
        *,
        memory: ContextMemory | None,
        actor_id: str,
        operation: str,
        before: ContextMemory | dict[str, Any] | None,
        after: ContextMemory | dict[str, Any] | None,
        turn_context: dict[str, Any],
        reason: str,
    ) -> None:
        session.add(
            ContextMemoryAudit(
                id=uuid.uuid4(),
                memory_id=memory.id if memory else None,
                user_id=(memory.user_id if memory else None),
                operation=operation,
                actor=str(actor_id),
                turn_context=turn_context,
                before_snapshot=_audit_snapshot(before),
                after_snapshot=_audit_snapshot(after),
                reason=reason,
            )
        )

    async def upsert_memory(
        self,
        *,
        actor_id: str,
        content: str,
        scope_type: str = "user",
        scope_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        memory_type: str = "fact",
        title: str | None = None,
        structured_data: dict[str, Any] | None = None,
        source_type: str = "manual",
        source_ref: str | None = None,
        confidence: float = 1.0,
        importance: int = 5,
        trust_level: str | None = None,
        evidence_refs: Iterable[Any] | Mapping[str, Any] | str | bytes | None = None,
        evidence_span: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
        status: str | None = None,
        is_pinned: bool = False,
        expires_at: datetime | None = None,
        created_by_actor: str | None = None,
        projection_metadata: dict[str, Any] | None = None,
        migration_id: str | None = None,
        turn_context: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
        idempotency_key: str | None = None,
        evidence_max_created_at: datetime | str | None = None,
        expected_replaces_id: str | None = None,
        source_session_id: str | uuid.UUID | None = None,
        source_message_id: str | uuid.UUID | None = None,
        source_user_input: str | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        clean_content = str(content or "").strip()
        if not clean_content:
            raise ScopedMemoryValidationError("memory content is required")
        scope = _scope(
            actor_id=str(actor_id),
            scope_type=scope_type,
            scope_id=scope_id,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
        )
        memory_type = str(memory_type or "fact").strip().casefold()[:32]
        source_type_key = str(source_type or "manual").strip().casefold()
        automatic_source = _is_automatic_memory_source(source_type_key)
        # Materialize the iterable once.  The deep sensitivity classifier
        # below must not consume a generator and silently remove the same
        # evidence from the persisted provenance list.
        raw_evidence_refs = (
            [evidence_refs]
            if isinstance(evidence_refs, (Mapping, str, bytes))
            else list(evidence_refs or ())
        )
        # The adapters perform the same deep check, but ``upsert_memory`` is
        # the canonical mutation boundary and can also be called directly by
        # a provider/integration.  Inspect every provider-controlled field so
        # a secret hidden in a title, structured payload, evidence span, or
        # provenance metadata cannot survive as an active/candidate row.
        sensitivity, rejection_reason = classify_sensitivity_fields(
            clean_content,
            title,
            structured_data,
            raw_evidence_refs,
            evidence_span,
            projection_metadata,
            source_ref,
            source_user_input,
        )
        effective_status = str(
            status or ("candidate" if source_type_key.endswith("_auto") else "active")
        )
        if rejection_reason:
            effective_status = "rejected"
        context = _turn_context(turn_context, tool_call_id)
        context.setdefault("user_id", str(actor_id))
        key = _dedupe_key(clean_content, memory_type, dedupe_key)
        effective_idempotency_key = str(idempotency_key or "").strip()
        if not effective_idempotency_key:
            effective_idempotency_key = (
                "implicit:"
                + _digest(
                    f"{actor_id}:{scope.scope_type}:{scope.scope_id}:"
                    f"{source_type}:{key}"
                )
            )
        effective_source_ref = str(source_ref or "").strip() or (
            f"{source_type}:{effective_idempotency_key}"
        )

        # Automatic Project Memory is allowed to persist only when at least
        # one citation can be resolved to a server-owned row at the final
        # storage boundary.  Opaque integration IDs and the synthetic
        # ``source_ref`` fallback are lineage only; they cannot authorize a
        # durable Project mutation.  A direct fast-path source message is
        # canonicalized here as chat evidence before the fallback is built.
        automatic_project = scope.scope_type == "project" and automatic_source
        bound_evidence = _project_memory_db_verifiable_evidence_ids(
            {
                "evidence_refs": raw_evidence_refs,
                "structured_data": structured_data,
                "projection_metadata": projection_metadata,
            }
        )
        source_message_uuid = _uuid(source_message_id)
        if source_message_uuid is not None:
            bound_evidence.add(f"chat:{source_message_uuid}")
        # Preserve every explicit identity accepted by the provenance parser.
        # Provider adapters may supply Mapping implementations, scalar
        # source-qualified refs, or payload-level ``evidence_ids``.  Materialize
        # all of them before the automatic Project gates so an identity cannot
        # authorize a write and then disappear behind the synthetic source_ref.
        evidence = _canonical_persisted_project_memory_evidence_refs(
            raw_evidence_refs,
            structured_data=structured_data,
            projection_metadata=projection_metadata,
            source_message_id=source_message_id,
        )
        if not evidence:
            evidence = [
                {
                    "type": str(source_type or "manual"),
                    "source_ref": effective_source_ref,
                }
            ]
        if automatic_project and source_message_uuid is not None:
            # Persist the direct source identity as explicit evidence as well
            # as validating it through ``source_session_id`` below.  This
            # keeps later reconciliation/forget operations DB-verifiable even
            # when the original adapter omitted ``evidence_refs``.
            canonical_source_message = f"chat:{source_message_uuid}"
            if canonical_source_message not in _project_memory_db_verifiable_evidence_ids(
                {"evidence_refs": evidence}
            ):
                evidence.append(
                    {
                        "type": "chat",
                        "evidence_id": canonical_source_message,
                    }
                )
        metadata = dict(projection_metadata or {})
        metadata["idempotency_key"] = effective_idempotency_key
        metadata["turn_context"] = context
        incoming_evidence_at = _coerce_temporal_timestamp(evidence_max_created_at)
        if incoming_evidence_at is None:
            incoming_evidence_at = _evidence_max_created_at(evidence, structured_data)
        if incoming_evidence_at is not None:
            dreaming_metadata = dict(metadata.get("dreaming") or {})
            dreaming_metadata["evidence_max_created_at"] = incoming_evidence_at.isoformat()
            metadata["dreaming"] = dreaming_metadata

        lock_material = f"{actor_id}:{scope.scope_type}:{scope.scope_id}:{key}"
        async with self._serialized_dedupe_session(lock_material) as session:
            if scope.scope_type == "project" and automatic_source:
                # Every automatic Project write must bind to a live Project,
                # even when a malformed provider omits provenance entirely.
                # Never let an empty/legacy payload bypass the completed or
                # deleted Project gate.
                await self._require_active_project_for_auto(
                    session,
                    actor_id=str(actor_id),
                    project_id=scope.project_id,
                )
                if not bound_evidence:
                    raise ScopedMemoryValidationError(
                        "automatic Project Memory requires database-bound evidence"
                    )
            if scope.scope_type == "project" and (
                source_session_id is not None or source_message_id is not None
            ):
                await self._require_project_chat_binding(
                    session,
                    actor_id=str(actor_id),
                    project_id=scope.project_id,
                    session_id=source_session_id,
                    source_message_id=source_message_id,
                    source_user_input=source_user_input,
                )
            if scope.scope_type == "project" and automatic_source:
                await self._require_project_chat_evidence(
                    session,
                    actor_id=str(actor_id),
                    project_id=scope.project_id,
                    evidence_refs=evidence,
                    structured_data=structured_data,
                    projection_metadata=metadata,
                )
                await self._require_project_nonchat_evidence(
                    session,
                    actor_id=str(actor_id),
                    project_id=scope.project_id,
                    evidence_refs=evidence,
                    structured_data=structured_data,
                    projection_metadata=metadata,
                )
            await self._require_scope_permission(
                session, scope=scope, actor_id=str(actor_id), write=True
            )
            existing_rows = list(
                (
                    await session.execute(
                        select(ContextMemory)
                        .where(
                            ContextMemory.user_id == str(actor_id),
                            ContextMemory.scope_type == scope.scope_type,
                            ContextMemory.scope_id == scope.scope_id,
                            ContextMemory.dedupe_key == key,
                            ContextMemory.status.in_(tuple(ACTIVE_STATUSES)),
                        )
                        .order_by(ContextMemory.version.desc())
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            # A delayed backfill/retry must never replace an active memory
            # whose source evidence is newer.  The gate is limited to
            # Dreaming/history writes and only applies when both sides expose
            # a source timestamp; legacy/manual rows remain compatible.
            if incoming_evidence_at is not None and str(source_type or "").casefold().startswith(
                ("dreaming", "history_consolidation")
            ):
                latest_existing_at = max(
                    (
                        timestamp
                        for row in existing_rows
                        for timestamp in (
                            _evidence_max_created_at(
                                getattr(row, "evidence_refs", None),
                                getattr(row, "structured_data", None),
                                getattr(row, "projection_metadata", None),
                            ),
                        )
                        if timestamp is not None
                    ),
                    default=None,
                )
                if latest_existing_at is not None and incoming_evidence_at <= latest_existing_at:
                    retained = next(
                        (row for row in existing_rows if row.status == "active"),
                        existing_rows[0] if existing_rows else None,
                    )
                    if retained is not None:
                        return self._result(
                            retained,
                            operation="unchanged",
                            reason=(
                                "temporal_precedence_duplicate"
                                if incoming_evidence_at == latest_existing_at
                                else "temporal_precedence_stale"
                            ),
                        )
            idempotent_rows = list(
                (
                    await session.execute(
                        select(ContextMemory)
                        .where(
                            ContextMemory.user_id == str(actor_id),
                            ContextMemory.scope_type == scope.scope_type,
                            ContextMemory.scope_id == scope.scope_id,
                            ContextMemory.status.in_(tuple(ACTIVE_STATUSES)),
                        )
                        .order_by(ContextMemory.updated_at.desc())
                        .limit(500)
                    )
                )
                .scalars()
                .all()
            )
            for existing in idempotent_rows:
                if (existing.projection_metadata or {}).get(
                    "idempotency_key"
                ) == effective_idempotency_key:
                    existing_evidence_at = _evidence_max_created_at(
                        getattr(existing, "evidence_refs", None),
                        getattr(existing, "structured_data", None),
                        getattr(existing, "projection_metadata", None),
                    )
                    # An implicit key is stable across retries, but a newer
                    # Dreaming observation is an intentional replacement,
                    # not a replay of the old generation.
                    if (
                        incoming_evidence_at is not None
                        and str(source_type or "")
                        .casefold()
                        .startswith(("dreaming", "history_consolidation"))
                        and (
                            existing_evidence_at is None
                            or incoming_evidence_at > existing_evidence_at
                        )
                    ):
                        continue
                    return self._result(
                        existing,
                        operation="unchanged",
                        reason="idempotency_key_replayed",
                    )
            identical = next(
                (
                    item
                    for item in existing_rows
                    if _normalized_text(item.content) == _normalized_text(clean_content)
                    and item.status == effective_status
                ),
                None,
            )
            if identical is not None:
                return self._result(
                    identical,
                    operation="unchanged",
                    reason="same_scope_dedupe_match",
                )

            replaced = next((item for item in existing_rows if item.status == "active"), None)
            if replaced is None:
                replaced = next((item for item in existing_rows if item.status == "candidate"), None)
            if expected_replaces_id is not None:
                expected_uuid = _uuid(expected_replaces_id)
                if expected_uuid is None or replaced is None or replaced.id != expected_uuid:
                    raise ScopedMemoryConflict("memory changed before background replacement")
            version = (int(replaced.version or 1) + 1) if replaced else 1
            now = datetime.utcnow()
            replaced_overview_project_id = self._overview_active_project_id(replaced)
            replaced_before = _audit_snapshot(replaced)
            if replaced is not None:
                replaced.status = "superseded"
                replaced.updated_at = now
                await session.flush()

            memory = ContextMemory(
                id=uuid.uuid4(),
                user_id=str(actor_id),
                project_id=scope.project_id,
                task_id=scope.task_id,
                session_id=scope.session_id,
                scope_type=scope.scope_type,
                scope_id=scope.scope_id,
                memory_type=memory_type,
                title=(str(title).strip()[:200] if title else None),
                content=clean_content,
                structured_data=dict(structured_data or {}),
                source_type=str(source_type or "manual")[:32],
                source_ref=effective_source_ref,
                confidence=max(0.0, min(float(confidence), 1.0)),
                importance=max(1, min(int(importance), 10)),
                trust_level=str(
                    trust_level
                    or ("verified" if source_type in {"manual", "correction"} else "inferred")
                )[:32],
                sensitivity=sensitivity,
                evidence_refs=evidence,
                evidence_span=dict(evidence_span or {}),
                dedupe_key=key,
                supersedes_id=replaced.id if replaced else None,
                version=version,
                created_by_actor=str(created_by_actor or actor_id)[:120],
                rejection_reason=rejection_reason,
                projection_metadata=metadata,
                migration_id=migration_id,
                status=effective_status,
                is_pinned=bool(is_pinned),
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            session.add(memory)
            await session.flush()
            reason = rejection_reason or (
                "superseded_same_scope_memory" if replaced else "new_scoped_memory"
            )
            self._add_audit(
                session,
                memory=memory,
                actor_id=str(actor_id),
                operation="rejected" if rejection_reason else ("superseded" if replaced else "created"),
                before=replaced_before,
                after=memory,
                turn_context=context,
                reason=reason,
            )
            await session.commit()
            await session.refresh(memory)
            result = self._result(
                memory,
                operation="rejected" if rejection_reason else ("superseded" if replaced else "created"),
                reason=reason,
                replaced_id=replaced.id if replaced else None,
            )
            overview_project_ids = [replaced_overview_project_id]
            if memory.status != "rejected":
                overview_project_ids.append(
                    self._overview_active_project_id(memory),
                )
        await self._enqueue_project_overview_refresh_best_effort(
            project_ids=overview_project_ids,
            actor_id=str(actor_id),
            reason="scoped_memory_upsert",
        )
        return result

    async def replace_memory_from_dreaming(
        self,
        *,
        actor_id: str,
        content: str,
        existing_memory_id: str | None = None,
        memory_type: str = "fact",
        title: str | None = None,
        structured_data: dict[str, Any] | None = None,
        evidence_refs: Iterable[Any] | Mapping[str, Any] | str | bytes | None = None,
        evidence_span: dict[str, Any] | str | None = None,
        evidence_max_created_at: datetime | str | None = None,
        evidence_count: int | None = None,
        confidence: float = 0.0,
        importance: int = 5,
        source_type: str = "dreaming_auto",
        source_ref: str | None = None,
        run_id: str | None = None,
        status: str = "active",
        idempotency_key: str | None = None,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        """Persist a Dreaming replacement through the canonical write path.

        Consolidation is allowed to replace an existing generation, but it
        must retain lineage and source provenance.  ``upsert_memory`` handles
        the transactional supersession; this wrapper supplies the stable
        Dreaming metadata and reuses the old generation's dedupe key when an
        explicit target is provided.
        """
        actor_id = _canonical_actor_id(actor_id)
        source = str(source_type or "dreaming_auto").strip()[:32]
        if source in {"manual", "correction", "manual_update"}:
            source = "dreaming_auto"
        clean_content = str(content or "").strip()
        if not clean_content:
            raise ScopedMemoryValidationError("memory content is required")

        target = None
        if existing_memory_id and str(status or "active").casefold() == "active":
            target = await self.get_memory(str(existing_memory_id), actor_id=str(actor_id))
            if str(target.get("status") or "") not in ACTIVE_STATUSES:
                return {
                    "success": True,
                    "memory_id": target.get("id"),
                    "scope": target.get("scope_type"),
                    "scope_id": target.get("scope_id"),
                    "operation": "unchanged",
                    "replaced_id": None,
                    "reason": "target_memory_not_active",
                    "memory": target,
                }
            if bool(target.get("is_pinned")) or str(target.get("source_type") or "") in {
                "manual",
                "manual_update",
                "correction",
                "candidate_approval",
            }:
                return {
                    "success": True,
                    "memory_id": target.get("id"),
                    "scope": target.get("scope_type"),
                    "scope_id": target.get("scope_id"),
                    "operation": "unchanged",
                    "replaced_id": None,
                    "reason": "explicit_memory_precedence",
                    "memory": target,
                }
            # A replacement must stay on the predecessor's dedupe lineage;
            # callers cannot bypass temporal precedence by supplying a new
            # key for the same memory_id.
            dedupe_key = target.get("dedupe_key") or dedupe_key


        metadata = dict(structured_data or {})
        dreaming_metadata = dict(metadata.get("dreaming") or {})
        dreaming_metadata["mode"] = dreaming_metadata.get("mode") or "history_consolidation"
        if run_id:
            dreaming_metadata["run_id"] = str(run_id)
        if evidence_count is not None:
            try:
                dreaming_metadata["evidence_count"] = max(0, int(evidence_count))
            except (TypeError, ValueError):
                dreaming_metadata["evidence_count"] = 0
        parsed_evidence_at = _coerce_temporal_timestamp(evidence_max_created_at)
        if parsed_evidence_at is not None:
            dreaming_metadata["evidence_max_created_at"] = parsed_evidence_at.isoformat()
        metadata["dreaming"] = dreaming_metadata
        refs = _canonical_persisted_project_memory_evidence_refs(
            evidence_refs,
            structured_data=metadata,
        )
        if parsed_evidence_at is not None:
            refs.append({"type": "dreaming", "created_at": parsed_evidence_at.isoformat()})
        span = (
            dict(evidence_span)
            if isinstance(evidence_span, dict)
            else {"text": str(evidence_span)}
            if evidence_span not in (None, "")
            else {}
        )
        return await self.upsert_memory(
            actor_id=str(actor_id),
            content=clean_content,
            scope_type=str((target or {}).get("scope_type") or "user"),
            scope_id=str((target or {}).get("scope_id") or actor_id),
            project_id=(target or {}).get("project_id"),
            task_id=(target or {}).get("task_id"),
            session_id=(target or {}).get("session_id"),
            memory_type=memory_type,
            title=title,
            structured_data=metadata,
            source_type=source,
            source_ref=source_ref or (f"dreaming:{run_id}" if run_id else None),
            confidence=confidence,
            importance=importance,
            trust_level="verified",
            evidence_refs=refs,
            evidence_span=span,
            dedupe_key=dedupe_key,
            status=status,
            evidence_max_created_at=parsed_evidence_at,
            idempotency_key=idempotency_key,
            expected_replaces_id=(str(target.get("id")) if target else None),
        )

    async def get_memory(self, memory_id: str, *, actor_id: str) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            memory = await session.get(ContextMemory, _uuid(memory_id))
            if memory is None:
                raise ScopedMemoryNotFound("memory not found")
            await self._require_scope_permission(
                session,
                scope=self._scope_from_memory(memory),
                actor_id=str(actor_id),
                write=False,
            )
            return memory.to_dict()

    async def list_memories(
        self,
        *,
        actor_id: str,
        scope_type: str | None = None,
        scope_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        include_history: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            stmt = select(ContextMemory)
            if project_id:
                project_uuid = _uuid(project_id)
                if not project_uuid:
                    raise ScopedMemoryValidationError("invalid project id")
                await self._require_project_permission(
                    session, project_id=project_uuid, actor_id=str(actor_id), write=False
                )
                # A Project query is a Project-scope read.  Do not expose a
                # User/Task/Session row that merely carries a denormalized
                # ``project_id`` (manual callers can still provide that
                # field for legacy compatibility).
                stmt = stmt.where(
                    ContextMemory.scope_type == "project",
                    ContextMemory.scope_id == str(project_uuid),
                    ContextMemory.project_id == project_uuid,
                )
            else:
                stmt = stmt.where(ContextMemory.user_id == str(actor_id))
            if task_id:
                stmt = stmt.where(ContextMemory.task_id == _uuid(task_id))
            if session_id:
                stmt = stmt.where(ContextMemory.session_id == _uuid(session_id))
            if scope_type:
                stmt = stmt.where(ContextMemory.scope_type == scope_type)
            if scope_id:
                stmt = stmt.where(ContextMemory.scope_id == str(scope_id))
            if status:
                stmt = stmt.where(ContextMemory.status == status)
            elif not include_history:
                stmt = stmt.where(ContextMemory.status.in_(("active", "candidate")))
            rows = (
                await session.execute(
                    stmt.order_by(
                        ContextMemory.is_pinned.desc(),
                        ContextMemory.importance.desc(),
                        ContextMemory.updated_at.desc(),
                    ).limit(max(1, min(int(limit), 1000)))
                )
            ).scalars().all()
            return [row.to_dict() for row in rows]

    async def find_project_memory_reconciliation_match(
        self,
        *,
        actor_id: str,
        project_id: str | uuid.UUID,
        evidence_ids: Iterable[Any] | None,
        semantic_identities: Iterable[Any] | None,
    ) -> dict[str, Any] | None:
        """Find one exact active Project Memory cross-path match.

        The conversation fast path and Project Steward intentionally keep
        independent provenance/source contracts.  This helper is therefore a
        *read-only* reconciliation probe, not a merge operation: it returns a
        row only when the requested Project, at least one canonical evidence
        identity, and at least one deterministic semantic identity all match.
        It never overwrites, forgets, promotes, changes lineage, or writes an
        audit row.  Callers decide whether to skip their own duplicate write.

        ``evidence_ids`` and ``semantic_identities`` are normalized locally so
        callers can pass collector JSON or already-canonical values.  Matching
        is exact set intersection; no text similarity or fuzzy fallback is
        allowed.  The query is bounded to the same 1000-row safety cap used by
        Project Steward's active-memory loader.
        """

        actor_id = _canonical_actor_id(actor_id)
        project_uuid = _uuid(project_id)
        if project_uuid is None:
            raise ScopedMemoryValidationError("invalid project id")

        requested_evidence: set[str] = set()
        raw_evidence_values = (
            [evidence_ids]
            if isinstance(evidence_ids, (str, bytes))
            else (evidence_ids or ())
        )
        for raw in raw_evidence_values:
            identity = canonical_project_memory_evidence_id(raw)
            if identity:
                requested_evidence.add(identity)

        requested_semantic: set[str] = set()
        raw_semantic_values = (
            [semantic_identities]
            if isinstance(semantic_identities, (str, bytes))
            else (semantic_identities or ())
        )
        for raw in raw_semantic_values:
            if raw in (None, ""):
                continue
            text_value = str(raw).strip()
            if not text_value:
                continue
            identity = project_memory_semantic_identity(text_value)
            if identity:
                requested_semantic.add(identity)

        # Both dimensions are required.  Returning early avoids touching the
        # database for malformed/underspecified probes and prevents a caller
        # from accidentally treating an evidence-only or semantic-only match
        # as the same durable concept.
        if not requested_evidence or not requested_semantic:
            return None

        async with await self._new_session() as session:
            await self._require_project_permission(
                session,
                project_id=project_uuid,
                actor_id=str(actor_id),
                write=False,
            )
            rows = list(
                (
                    await session.execute(
                        select(ContextMemory)
                        .where(
                            ContextMemory.scope_type == "project",
                            ContextMemory.project_id == project_uuid,
                            ContextMemory.scope_id == str(project_uuid),
                            ContextMemory.status == "active",
                        )
                        .order_by(
                            ContextMemory.updated_at.desc(),
                            ContextMemory.id.desc(),
                        )
                        # Fetch one row beyond the safe cap so a truncated
                        # result cannot be mistaken for a complete active set.
                        .limit(1001)
                    )
                )
                .scalars()
                .all()
            )

        if len(rows) > 1000:
            raise ScopedMemoryConflict(
                "Project Scoped Memory set exceeds safe reconciliation limit"
            )

        for row in rows:
            row_evidence = project_memory_evidence_ids(row)
            if not row_evidence.intersection(requested_evidence):
                continue
            row_semantic = _project_memory_semantic_identities(row)
            if not row_semantic.intersection(requested_semantic):
                continue
            return row.to_dict()
        return None

    # Keep a concise alias for callers that describe this operation as an
    # "exact match" probe.  Both names remain read-only and share one code
    # path so their behavior cannot drift.
    find_exact_project_memory_match = find_project_memory_reconciliation_match

    async def upsert_project_memory_reconciled(
        self,
        *,
        actor_id: str,
        project_id: str | uuid.UUID,
        evidence_ids: Iterable[Any] | None,
        semantic_identities: Iterable[Any] | None,
        upsert_kwargs: Mapping[str, Any],
        source_session_id: str | uuid.UUID | None = None,
        source_message_id: str | uuid.UUID | None = None,
        source_user_input: str | None = None,
    ) -> dict[str, Any]:
        """Atomically reconcile an exact Project fact before writing it.

        The fast Dreaming path and Project Steward intentionally retain
        different provenance/namespace and dedupe contracts.  This shared
        boundary is the only place where either producer may perform the
        cross-path ``probe -> upsert`` sequence, preventing concurrent
        workers from both observing a miss and creating duplicate active
        rows.  A matching row is returned unchanged; it is never re-sourced,
        re-scoped, forgotten, or audited by reconciliation.
        """

        actor_id = _canonical_actor_id(actor_id)
        project_uuid = _uuid(project_id)
        if project_uuid is None:
            raise ScopedMemoryValidationError("invalid project id")
        # ``upsert_memory`` accepts any iterable for ``evidence_refs``.  The
        # reconciliation boundary inspects the payload more than once, so
        # materialize one-shot iterables here and reuse the normalized copy
        # for both evidence authorization and durable persistence.  Without
        # this, a generator could authorize neither path consistently (or be
        # consumed by the first inspection and disappear before the write).
        normalized_upsert_kwargs = dict(upsert_kwargs)
        payload_evidence_refs = normalized_upsert_kwargs.get("evidence_refs")
        if isinstance(payload_evidence_refs, (Mapping, str, bytes)):
            payload_evidence_refs = [payload_evidence_refs]
        elif payload_evidence_refs is not None and not isinstance(
            payload_evidence_refs, (list, tuple, set, frozenset)
        ):
            try:
                payload_evidence_refs = list(payload_evidence_refs)
            except TypeError:
                # Preserve malformed scalar values so the canonical parser
                # fails closed instead of coercing arbitrary objects.
                pass
        if payload_evidence_refs is not normalized_upsert_kwargs.get("evidence_refs"):
            normalized_upsert_kwargs["evidence_refs"] = payload_evidence_refs
        upsert_kwargs = normalized_upsert_kwargs
        automatic_project = _is_automatic_memory_source(
            upsert_kwargs.get("source_type")
        )

        raw_evidence = (
            [evidence_ids]
            if isinstance(evidence_ids, (str, bytes))
            else list(evidence_ids or ())
        )
        canonical_evidence = sorted(
            {
                identity
                for raw in raw_evidence
                for identity in (canonical_project_memory_evidence_id(raw),)
                if identity
            }
        )
        # Treat explicit provenance embedded in the writer payload as an
        # equivalent canonical input.  This keeps the reconciliation boundary
        # strict even for adapters that supplied ``evidence_refs`` but omitted
        # the parallel ``evidence_ids`` argument.
        if automatic_project:
            payload_evidence = project_memory_evidence_ids(
                {
                    "evidence_refs": payload_evidence_refs,
                    "structured_data": upsert_kwargs.get("structured_data"),
                    "projection_metadata": upsert_kwargs.get("projection_metadata"),
                }
            )
            canonical_evidence = sorted(
                set(canonical_evidence).union(payload_evidence)
            )
            source_message_uuid = _uuid(source_message_id)
            if source_message_uuid is not None:
                canonical_chat_id = f"chat:{source_message_uuid}"
                if canonical_chat_id not in canonical_evidence:
                    canonical_evidence.append(canonical_chat_id)
                    canonical_evidence.sort()
        raw_semantics = (
            [semantic_identities]
            if isinstance(semantic_identities, (str, bytes))
            else list(semantic_identities or ())
        )
        canonical_semantics = sorted(
            {
                identity
                for raw in raw_semantics
                if raw not in (None, "")
                for identity in (project_memory_semantic_identity(raw),)
                if identity
            }
        )

        async def write_without_reconciliation(
            *,
            expected_replaces_id: str | None = None,
        ) -> dict[str, Any]:
            write_kwargs = dict(upsert_kwargs)
            # The boundary owns the Project scope.  Ignore duplicate scope
            # values supplied by an adapter rather than allowing a caller to
            # redirect a Project write through this method.
            for key in (
                "actor_id",
                "scope_type",
                "scope_id",
                "project_id",
                "task_id",
                "session_id",
                "source_session_id",
                "source_message_id",
                "source_user_input",
            ):
                write_kwargs.pop(key, None)
            # ``evidence_ids`` is the canonical reconciliation input.  Older
            # adapters occasionally omitted the matching ``evidence_refs``
            # from ``upsert_kwargs``; retain their payload while adding only
            # the missing explicit identities so the writer-side Task/Docs
            # and chat provenance gates cannot be bypassed by omission.
            if canonical_evidence:
                existing_evidence = write_kwargs.get("evidence_refs")
                existing_evidence = (
                    list(existing_evidence)
                    if isinstance(existing_evidence, (list, tuple, set, frozenset))
                    else [existing_evidence]
                    if isinstance(existing_evidence, (Mapping, str, bytes))
                    else []
                )
                existing_ids = project_memory_evidence_ids(
                    {"evidence_refs": existing_evidence}
                )
                for evidence_id in canonical_evidence:
                    if evidence_id not in existing_ids:
                        existing_evidence.append(
                            {
                                "type": "project_steward",
                                "evidence_id": evidence_id,
                            }
                        )
                if existing_evidence:
                    write_kwargs["evidence_refs"] = existing_evidence
            if expected_replaces_id is not None:
                # A same-Steward semantic/evidence match may be a genuine
                # newer source revision rather than a replay.  Keep the
                # optimistic replacement guard inside the canonical writer;
                # callers cannot replace a row that changed after the probe.
                write_kwargs["expected_replaces_id"] = expected_replaces_id
            return await self.upsert_memory(
                actor_id=actor_id,
                scope_type="project",
                scope_id=str(project_uuid),
                project_id=str(project_uuid),
                source_session_id=source_session_id,
                source_message_id=source_message_id,
                source_user_input=source_user_input,
                **write_kwargs,
            )

        if automatic_project and not any(
            identity.split(":", 1)[0].casefold()
            in _DB_VERIFIABLE_PROJECT_EVIDENCE_PREFIXES
            for identity in canonical_evidence
        ):
            # Legacy/external automatic ingress without a DB-verifiable bound
            # citation must fail closed.  In particular, do not let the
            # synthetic ``source_ref`` fallback turn an opaque ``job:<id>``
            # into a Project Memory row.
            raise ScopedMemoryValidationError(
                "automatic Project Memory requires database-bound evidence"
            )

        # Legacy/external ingress without both canonical identity dimensions
        # cannot be safely reconciled.  Preserve the established write path
        # without inventing a semantic identity; automatic sources have
        # already passed the stricter evidence gate above.
        if not canonical_evidence or not canonical_semantics:
            return await write_without_reconciliation()

        async with self._serialized_project_reconciliation_guard(
            project_id=project_uuid,
            evidence_ids=canonical_evidence,
        ):
            # Reconciliation failures are correctness failures.  Propagate
            # ACL/DB/decryption/bounded-read errors so callers retry rather
            # than silently creating a duplicate Project Memory.
            existing = await self.find_project_memory_reconciliation_match(
                actor_id=actor_id,
                project_id=str(project_uuid),
                evidence_ids=canonical_evidence,
                semantic_identities=canonical_semantics,
            )
            if existing is not None:
                incoming_source_type = str(
                    upsert_kwargs.get("source_type") or ""
                ).strip().casefold()
                existing_source_type = str(
                    existing.get("source_type") or ""
                ).strip().casefold()
                incoming_structured = upsert_kwargs.get("structured_data")
                incoming_structured = (
                    incoming_structured
                    if isinstance(incoming_structured, Mapping)
                    else {}
                )
                existing_structured = existing.get("structured_data")
                existing_structured = (
                    existing_structured
                    if isinstance(existing_structured, Mapping)
                    else {}
                )
                incoming_namespace = str(
                    incoming_structured.get("namespace") or ""
                ).strip()
                existing_namespace = str(
                    existing_structured.get("namespace") or ""
                ).strip()
                incoming_semantic_key = str(
                    incoming_structured.get("semantic_key") or ""
                ).strip()
                existing_semantic_key = str(
                    existing_structured.get("semantic_key") or ""
                ).strip()
                incoming_evidence_at = _coerce_temporal_timestamp(
                    upsert_kwargs.get("evidence_max_created_at")
                ) or _evidence_max_created_at(
                    upsert_kwargs.get("evidence_refs"),
                    upsert_kwargs.get("structured_data"),
                    upsert_kwargs.get("projection_metadata"),
                )
                existing_evidence_at = _evidence_max_created_at(
                    existing.get("evidence_refs"),
                    existing.get("structured_data"),
                    existing.get("projection_metadata"),
                )
                # Project Steward's stable semantic key is also its update
                # identity.  Only a newer, materially changed Steward
                # observation in the same namespace may write through the
                # reconciliation no-op.  Non-Steward rows remain immutable
                # from the Steward path, preserving the source/trust boundary.
                steward_revision = (
                    incoming_source_type == "project_steward"
                    and existing_source_type == "project_steward"
                    and bool(incoming_namespace)
                    and incoming_namespace == existing_namespace
                    and bool(incoming_semantic_key)
                    and incoming_semantic_key.casefold()
                    == existing_semantic_key.casefold()
                    and _normalized_text(existing.get("content"))
                    != _normalized_text(upsert_kwargs.get("content"))
                    and incoming_evidence_at is not None
                    and (
                        existing_evidence_at is None
                        or incoming_evidence_at > existing_evidence_at
                    )
                )
                if steward_revision:
                    return await write_without_reconciliation(
                        expected_replaces_id=str(
                            existing.get("id") or existing.get("memory_id")
                        )
                    )
                memory_id = str(existing.get("id") or existing.get("memory_id") or "")
                return {
                    "success": True,
                    "memory_id": memory_id,
                    "scope": existing.get("scope_type") or "project",
                    "scope_id": existing.get("scope_id") or str(project_uuid),
                    "operation": "unchanged",
                    "replaced_id": None,
                    "reason": "project_evidence_semantic_reconciled",
                    "memory": existing,
                }
            return await write_without_reconciliation()

    async def get_settings(
        self,
        *,
        actor_id: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            actor_uuid = _uuid(actor_id)
            user = await session.get(User, actor_uuid) if actor_uuid else None
            principal = None
            if actor_uuid:
                if user is None:
                    raise ScopedMemoryPermissionDenied("authenticated user not found")
                user_settings = user.user_settings if isinstance(user.user_settings, dict) else {}
            else:
                # External principals are first-class owners, not users.  A
                # missing row intentionally means the safe default (enabled)
                # and does not create a synthetic account on a read path.
                principal = await session.get(ScopedMemoryPrincipal, actor_id)
                user_settings = (
                    principal.settings
                    if principal is not None and isinstance(principal.settings, dict)
                    else {}
                )
            memory_settings = (
                dict(user_settings.get("scoped_memory") or {})
                if isinstance(user_settings.get("scoped_memory"), dict)
                else {}
            )
            project_enabled: bool | None = None
            if project_id:
                project_uuid = _uuid(project_id)
                if not project_uuid:
                    raise ScopedMemoryValidationError("invalid project id")
                await self._require_project_permission(
                    session,
                    project_id=project_uuid,
                    actor_id=str(actor_id),
                    write=False,
                )
                project = await session.get(Project, project_uuid)
                metadata = project.project_metadata if isinstance(project.project_metadata, dict) else {}
                raw_project_enabled = metadata.get(
                    "scoped_memory_auto_enabled",
                    memory_settings.get("auto_enabled", True),
                )
                # Settings are a consent boundary.  Preserve the historical
                # enabled-by-default behavior only when the value is absent;
                # malformed/truthy strings (for example ``"false"``) must
                # not silently enable Project Memory.
                project_enabled = (
                    raw_project_enabled
                    if isinstance(raw_project_enabled, bool)
                    else None
                )
            raw_user_enabled = memory_settings.get("auto_enabled", True)
            user_enabled = (
                raw_user_enabled if isinstance(raw_user_enabled, bool) else False
            )
            return {
                "user_auto_enabled": user_enabled,
                "project_auto_enabled": project_enabled,
                "project_id": str(project_id) if project_id else None,
            }

    async def validate_project_evidence(
        self,
        *,
        actor_id: str,
        project_id: str | uuid.UUID,
        evidence_ids: Iterable[Any] | None,
    ) -> bool:
        """Validate live Task/Docs/chat evidence for a read-only side effect.

        Project Steward questions do not write a ``ContextMemory`` row, so
        they cannot rely on ``upsert_memory``'s same-transaction provenance
        gate.  This bounded read-only entry point lets the Steward recheck
        every canonical evidence identity immediately before exposing a
        question; unexpected database failures propagate for retry rather
        than being mistaken for an empty/stale result.
        """

        canonical_ids = [
            identity
            for raw in evidence_ids or ()
            for identity in (canonical_project_memory_evidence_id(raw),)
            if identity
        ]
        if not canonical_ids:
            return False
        session = await self._new_session()
        try:
            evidence_refs = [
                {
                    "type": "project_steward",
                    "evidence_id": identity,
                }
                for identity in canonical_ids
            ]
            await self._require_project_chat_evidence(
                session,
                actor_id=str(actor_id),
                project_id=project_id,
                evidence_refs=evidence_refs,
            )
            await self._require_project_nonchat_evidence(
                session,
                actor_id=str(actor_id),
                project_id=project_id,
                evidence_refs=evidence_refs,
            )
            return True
        except (ScopedMemoryPermissionDenied, ScopedMemoryValidationError):
            return False
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result

    async def update_settings(
        self,
        *,
        actor_id: str,
        user_auto_enabled: bool | None = None,
        project_id: str | None = None,
        project_auto_enabled: bool | None = None,
    ) -> dict[str, Any]:
        if (
            user_auto_enabled is not None
            and not isinstance(user_auto_enabled, bool)
        ):
            raise ScopedMemoryValidationError(
                "user_auto_enabled must be boolean"
            )
        if (
            project_auto_enabled is not None
            and not isinstance(project_auto_enabled, bool)
        ):
            raise ScopedMemoryValidationError(
                "project_auto_enabled must be boolean"
            )
        actor_id = _canonical_actor_id(actor_id)
        project_uuid = None
        if project_auto_enabled is not None:
            project_uuid = _uuid(project_id)
            if not project_uuid:
                raise ScopedMemoryValidationError(
                    "project_id is required for project auto-memory setting"
                )
        async with await self._new_session() as session:
            # Keep settings updates in the same advisory/row-lock graph as
            # the final automatic-write gate.  The actor advisory also covers
            # first-time external-principal creation, where no row exists yet.
            await _lock_scoped_memory_actor(session, actor_id)
            project = None
            if project_uuid is not None:
                await _lock_scoped_memory_project(session, project_uuid)
                project = (
                    await session.execute(
                        select(Project)
                        .where(Project.id == project_uuid)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()

            # With a project setting, Project is deliberately locked before the
            # actor row.  Task/Docs ACL writers use the same parent-first order;
            # user-only changes still take the actor advisory before User.
            actor_uuid = _uuid(actor_id)
            user = None
            principal = None
            if actor_uuid:
                user = (
                    await session.execute(
                        select(User)
                        .where(User.id == actor_uuid)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if user is None:
                    raise ScopedMemoryPermissionDenied("authenticated user not found")
            else:
                principal = (
                    await session.execute(
                        select(ScopedMemoryPrincipal)
                        .where(ScopedMemoryPrincipal.principal_key == actor_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if principal is None:
                    principal = ScopedMemoryPrincipal(
                        principal_key=actor_id,
                        provider=actor_id.split(":", 1)[0].casefold()
                        if ":" in actor_id
                        else "external",
                        settings={},
                        metadata_json={},
                    )
                    session.add(principal)
            if user_auto_enabled is not None:
                settings = dict(
                    user.user_settings if actor_uuid and user is not None else principal.settings
                    if principal is not None
                    else {}
                )
                scoped = dict(settings.get("scoped_memory") or {})
                scoped["auto_enabled"] = user_auto_enabled
                settings["scoped_memory"] = scoped
                if actor_uuid and user is not None:
                    user.user_settings = settings
                elif principal is not None:
                    principal.settings = settings
            if project_auto_enabled is not None:
                await self._require_project_permission(
                    session,
                    project_id=project_uuid,
                    actor_id=str(actor_id),
                    write=True,
                )
                # ``project`` was read and locked above.  The permission helper
                # reuses that identity-map row and rechecks the live ACL before
                # changing metadata.
                metadata = dict(project.project_metadata or {})
                metadata["scoped_memory_auto_enabled"] = project_auto_enabled
                project.project_metadata = metadata
            context = _turn_context(None, None)
            context.setdefault("user_id", str(actor_id))
            session.add(
                ContextMemoryAudit(
                    id=uuid.uuid4(),
                    memory_id=None,
                    user_id=str(actor_id),
                    operation="settings_updated",
                    actor=str(actor_id),
                    turn_context=context,
                    before_snapshot={},
                    after_snapshot={
                        "user_auto_enabled": user_auto_enabled,
                        "project_id": str(project_id) if project_id else None,
                        "project_auto_enabled": project_auto_enabled,
                    },
                    reason="explicit_settings_update",
                )
            )
            await session.commit()
        return await self.get_settings(actor_id=str(actor_id), project_id=project_id)

    async def list_jobs(
        self,
        *,
        actor_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            rows = list(
                (
                    await session.execute(
                        select(ScopedMemoryJob)
                        .where(ScopedMemoryJob.user_id == str(actor_id))
                        .order_by(ScopedMemoryJob.created_at.desc())
                        .limit(max(1, min(int(limit), 200)))
                    )
                ).scalars().all()
            )
            return [
                {
                    "id": str(row.id),
                    "session_id": str(row.session_id),
                    "project_id": str(row.project_id) if row.project_id else None,
                    "status": row.status,
                    "attempts": row.attempts,
                    "error": row.error,
                    "next_retry_at": row.next_retry_at.isoformat() if row.next_retry_at else None,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row in rows
            ]

    async def list_dreaming_runs(
        self,
        *,
        actor_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return the authenticated user's Dreaming execution history.

        Dreaming runs intentionally contain only source identifiers and
        counters.  The conversation payload itself remains in the canonical
        conversation tables and is never copied into this user-facing list.
        """
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            rows = list(
                (
                    await session.execute(
                        select(DreamingMemoryRun)
                        .where(DreamingMemoryRun.user_id == str(actor_id))
                        .order_by(DreamingMemoryRun.created_at.desc())
                        .limit(max(1, min(int(limit), 200)))
                    )
                )
                .scalars()
                .all()
            )
            return [
                row.to_dict() if hasattr(row, "to_dict") else {
                    "id": str(row.id),
                    "user_id": row.user_id,
                    "trigger": row.trigger,
                    "status": row.status,
                    "source_message_ids": list(row.source_message_ids or []),
                    "source_count": row.source_count,
                    "source_digest": row.source_digest,
                    "backfill": bool(row.backfill),
                    "candidate_count": row.candidate_count,
                    "mutation_count": row.mutation_count,
                    "started_at": row.started_at.isoformat() if row.started_at else None,
                    "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                    "error": row.error,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row in rows
            ]

    async def get_dreaming_overview(self, *, actor_id: str) -> dict[str, Any]:
        """Build the privacy-safe per-user Dreaming overview projection.

        This method deliberately queries only ``scope_type='user'`` rows.  In
        particular, project/task/session memories are not mixed into a
        personal overview even when the caller has access to those scopes.
        Retrieval is read-only; all mutations continue to flow through this
        service's existing write methods.
        """
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            memories = list(
                (
                    await session.execute(
                        select(ContextMemory)
                        .where(
                            ContextMemory.user_id == str(actor_id),
                            ContextMemory.scope_type == "user",
                            ContextMemory.status.in_(("active", "candidate")),
                            or_(
                                ContextMemory.expires_at.is_(None),
                                ContextMemory.expires_at > datetime.utcnow(),
                            ),
                        )
                        .order_by(
                            ContextMemory.status.asc(),
                            ContextMemory.is_pinned.desc(),
                            ContextMemory.importance.desc(),
                            ContextMemory.updated_at.desc(),
                        )
                        .limit(1000)
                    )
                )
                .scalars()
                .all()
            )

            # State was introduced after the original Scoped Memory tables.
            # Keep the projection useful during a rolling migration where the
            # new table may not yet exist.
            state = None
            try:
                state = await session.get(DreamingMemoryState, str(actor_id))
            except Exception:
                logger.debug("Dreaming state table unavailable", exc_info=True)

            section_meta = {
                "fact": ("facts", "事実"),
                "facts": ("facts", "事実"),
                "preference": ("preferences", "好み"),
                "preferences": ("preferences", "好み"),
                "constraint": ("constraints", "制約"),
                "constraints": ("constraints", "制約"),
                "workflow": ("workflows", "ワークフロー"),
                "workflows": ("workflows", "ワークフロー"),
                "instruction": ("instructions", "指示"),
                "instructions": ("instructions", "指示"),
                "relationship": ("relationships", "関係"),
                "relationships": ("relationships", "関係"),
                "correction": ("corrections", "訂正"),
                "corrections": ("corrections", "訂正"),
            }
            sections_by_key: dict[str, dict[str, Any]] = {
                key: {"key": key, "label": label, "memories": []}
                for key, label in (
                    ("facts", "事実"),
                    ("preferences", "好み"),
                    ("constraints", "制約"),
                    ("workflows", "ワークフロー"),
                    ("instructions", "指示"),
                    ("relationships", "関係"),
                    ("corrections", "訂正"),
                    ("other", "その他"),
                )
            }
            active_count = 0
            candidate_count = 0
            for memory in memories:
                status = str(memory.status or "")
                if status == "active":
                    active_count += 1
                elif status == "candidate":
                    candidate_count += 1
                raw_key = str(memory.memory_type or "fact").strip().casefold() or "fact"
                key, label = section_meta.get(
                    raw_key,
                    ("other", "その他"),
                )
                section = sections_by_key.setdefault(
                    key,
                    {
                        "key": key,
                        "label": label,
                        "memories": [],
                    },
                )
                section["memories"].append(memory.to_dict())

            state_data = (
                state.to_dict()
                if state is not None and hasattr(state, "to_dict")
                else {}
            )
            backfill_complete = bool(state_data.get("backfill_complete", False))
            return {
                "active_count": active_count,
                "candidate_count": candidate_count,
                "last_dreamed_at": state_data.get("last_dreamed_at"),
                "backfill_complete": backfill_complete,
                "backfill_pending": not backfill_complete,
                "last_error": state_data.get("last_error"),
                "sections": list(sections_by_key.values()),
            }

    async def update_memory(
        self,
        memory_id: str,
        *,
        actor_id: str,
        changes: dict[str, Any],
        expected_version: int,
        turn_context: dict[str, Any] | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        allowed = {
            "content",
            "title",
            "memory_type",
            "structured_data",
            "confidence",
            "importance",
            "is_pinned",
            "expires_at",
            "trust_level",
            "evidence_refs",
            "evidence_span",
        }
        context = _turn_context(turn_context, tool_call_id)
        context.setdefault("user_id", str(actor_id))
        async with await self._new_session() as session:
            memory = (
                await session.execute(
                    select(ContextMemory)
                    .where(ContextMemory.id == _uuid(memory_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if memory is None:
                raise ScopedMemoryNotFound("memory not found")
            await self._require_scope_permission(
                session,
                scope=self._scope_from_memory(memory),
                actor_id=str(actor_id),
                write=True,
            )
            if int(memory.version or 1) != int(expected_version):
                raise ScopedMemoryConflict(
                    f"memory version changed: expected {expected_version}, current {memory.version}"
                )
            if memory.status not in {"active", "candidate"}:
                raise ScopedMemoryConflict(
                    f"memory is not editable in status {memory.status}"
                )
            source_overview_project_id = self._overview_active_project_id(memory)
            values = {key: getattr(memory, key) for key in allowed}
            values.update({key: value for key, value in changes.items() if key in allowed})
            content = str(values["content"] or "").strip()
            if not content:
                raise ScopedMemoryValidationError("memory content is required")
            sensitivity, rejection_reason = classify_sensitivity(content)
            before = _audit_snapshot(memory)
            now = datetime.utcnow()
            memory.status = "superseded"
            memory.updated_at = now
            await session.flush()
            replacement = ContextMemory(
                id=uuid.uuid4(),
                user_id=memory.user_id,
                project_id=memory.project_id,
                task_id=memory.task_id,
                session_id=memory.session_id,
                scope_type=memory.scope_type,
                scope_id=memory.scope_id,
                memory_type=str(values["memory_type"] or memory.memory_type)[:32],
                title=values["title"],
                content=content,
                structured_data=dict(values["structured_data"] or {}),
                source_type="manual_update",
                source_ref=memory.source_ref,
                confidence=max(0.0, min(float(values["confidence"]), 1.0)),
                importance=max(1, min(int(values["importance"]), 10)),
                trust_level=str(values["trust_level"] or memory.trust_level)[:32],
                sensitivity=sensitivity,
                evidence_refs=list(values["evidence_refs"] or []),
                evidence_span=dict(values["evidence_span"] or {}),
                dedupe_key=_dedupe_key(content, str(values["memory_type"] or memory.memory_type)),
                supersedes_id=memory.id,
                version=int(memory.version or 1) + 1,
                created_by_actor=str(actor_id),
                rejection_reason=rejection_reason,
                projection_metadata={"turn_context": context},
                status="rejected" if rejection_reason else "active",
                is_pinned=bool(values["is_pinned"]),
                expires_at=values["expires_at"],
                created_at=now,
                updated_at=now,
            )
            session.add(replacement)
            await session.flush()
            self._add_audit(
                session,
                memory=replacement,
                actor_id=str(actor_id),
                operation="updated",
                before=before,
                after=replacement,
                turn_context=context,
                reason="optimistic_lock_update",
            )
            await session.commit()
            await session.refresh(replacement)
            result = self._result(
                replacement,
                operation="updated",
                reason="optimistic_lock_update",
                replaced_id=memory.id,
            )
            overview_project_ids = [source_overview_project_id]
            if replacement.status != "rejected":
                overview_project_ids.append(
                    self._overview_active_project_id(replacement),
                )
        await self._enqueue_project_overview_refresh_best_effort(
            project_ids=overview_project_ids,
            actor_id=str(actor_id),
            reason="scoped_memory_updated",
        )
        return result

    async def forget_project_memory_reconciled(
        self,
        memory_id: str,
        *,
        actor_id: str,
        project_id: str | uuid.UUID,
        evidence_ids: Iterable[Any] | None,
        expected_version: int,
        namespace: str,
        reason: str = "project_steward_forget",
        turn_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Forget one Steward Project Memory under one final authorization gate.

        Project Steward's model plan is produced outside the mutation
        transaction.  This method is the canonical final boundary: it locks
        the live Project first, rechecks consent and actor ACL, locks and
        revalidates every cited chat/Task/Docs row, and only then locks the
        target ``ContextMemory`` before marking it forgotten.  No generic
        ``forget_memory`` call is made after releasing the evidence locks, so
        an evidence delete/rebind/ACL change cannot race a destructive status
        transition.
        """

        actor_id = _canonical_actor_id(actor_id)
        project_uuid = _uuid(project_id)
        if project_uuid is None:
            raise ScopedMemoryValidationError("invalid project id")
        clean_namespace = str(namespace or "").strip()
        if not clean_namespace or len(clean_namespace) > 200 or "\x00" in clean_namespace:
            raise ScopedMemoryValidationError("Project Steward namespace is required")

        canonical_evidence = sorted(
            {
                identity
                for raw in (
                    [evidence_ids]
                    if isinstance(evidence_ids, (str, bytes))
                    else list(evidence_ids or ())
                )
                for identity in (canonical_project_memory_evidence_id(raw),)
                if identity
            }
        )
        if not any(
            identity.split(":", 1)[0].casefold()
            in _DB_VERIFIABLE_PROJECT_EVIDENCE_PREFIXES
            for identity in canonical_evidence
        ):
            raise ScopedMemoryValidationError(
                "Project Steward forget requires database-bound evidence"
            )
        evidence_refs = [
            {
                "type": (
                    "chat"
                    if identity.split(":", 1)[0].casefold() == "chat"
                    else "project_steward"
                ),
                "evidence_id": identity,
            }
            for identity in canonical_evidence
        ]

        async with await self._new_session() as session:
            # This acquires the Project FOR UPDATE lock and performs the
            # explicit project consent check while the row is protected.
            await self._require_active_project_for_auto(
                session,
                actor_id=str(actor_id),
                project_id=project_uuid,
            )
            # Project ACL mutations use the same parent-first Project lock, so
            # this read observes the final committed actor permission.
            await self._require_project_permission(
                session,
                project_id=project_uuid,
                actor_id=str(actor_id),
                write=True,
            )
            # Both helpers retain the Project -> evidence-parent -> evidence
            # row lock order and execute within this same transaction.
            await self._require_project_chat_evidence(
                session,
                actor_id=str(actor_id),
                project_id=project_uuid,
                evidence_refs=evidence_refs,
            )
            await self._require_project_nonchat_evidence(
                session,
                actor_id=str(actor_id),
                project_id=project_uuid,
                evidence_refs=evidence_refs,
            )

            target_uuid = _uuid(memory_id)
            if target_uuid is None:
                raise ScopedMemoryNotFound("memory not found")
            memory = (
                await session.execute(
                    select(ContextMemory)
                    .where(ContextMemory.id == target_uuid)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if memory is None:
                raise ScopedMemoryNotFound("memory not found")
            if (
                str(getattr(memory, "scope_type", "") or "") != "project"
                or not _same_uuid(getattr(memory, "project_id", None), project_uuid)
                or not _same_uuid(getattr(memory, "scope_id", None), project_uuid)
            ):
                raise ScopedMemoryPermissionDenied(
                    "memory is outside Project scope"
                )
            if str(getattr(memory, "source_type", "") or "").strip().casefold() != (
                "project_steward"
            ):
                raise ScopedMemoryPermissionDenied(
                    "memory is outside Project Steward namespace"
                )
            structured_data = getattr(memory, "structured_data", None)
            if (
                not isinstance(structured_data, Mapping)
                or str(structured_data.get("namespace") or "").strip()
                != clean_namespace
            ):
                raise ScopedMemoryPermissionDenied(
                    "memory is outside Project Steward namespace"
                )
            if str(getattr(memory, "status", "") or "").strip().casefold() != "active":
                raise ScopedMemoryConflict("memory is no longer active")
            try:
                current_version = int(getattr(memory, "version", 1) or 1)
                requested_version = int(expected_version)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ScopedMemoryValidationError(
                    "invalid expected memory version"
                ) from exc
            if current_version != requested_version:
                raise ScopedMemoryConflict("memory version changed")

            context = _turn_context(turn_context, None)
            context.setdefault("user_id", str(actor_id))
            before = _audit_snapshot(memory)
            overview_project_id = self._overview_active_project_id(memory)
            memory.status = "forgotten"
            memory.version = current_version + 1
            memory.updated_at = datetime.utcnow()
            self._add_audit(
                session,
                memory=memory,
                actor_id=str(actor_id),
                operation="forgotten",
                before=before,
                after=memory,
                turn_context=context,
                reason=reason,
            )
            await session.commit()
            result = self._result(memory, operation="forgotten", reason=reason)

        await self._enqueue_project_overview_refresh_best_effort(
            project_ids=[overview_project_id],
            actor_id=str(actor_id),
            reason="scoped_memory_forgotten",
        )
        return result

    async def forget_memory(
        self,
        memory_id: str,
        *,
        actor_id: str,
        expected_version: int | None = None,
        full_deletion: bool = False,
        reason: str = "explicit_forget",
        turn_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            memory = (
                await session.execute(
                    select(ContextMemory)
                    .where(ContextMemory.id == _uuid(memory_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if memory is None:
                raise ScopedMemoryNotFound("memory not found")
            await self._require_scope_permission(
                session,
                scope=self._scope_from_memory(memory),
                actor_id=str(actor_id),
                write=True,
            )
            if expected_version is not None and int(memory.version or 1) != int(expected_version):
                raise ScopedMemoryConflict("memory version changed")
            context = _turn_context(turn_context, None)
            context.setdefault("user_id", str(actor_id))
            before = _audit_snapshot(memory)
            overview_project_id = self._overview_active_project_id(memory)
            if full_deletion:
                self._add_audit(
                    session,
                    memory=memory,
                    actor_id=str(actor_id),
                    operation="hard_deleted",
                    before=before,
                    after=None,
                    turn_context=context,
                    reason=reason,
                )
                await session.flush()
                await session.delete(memory)
                await session.commit()
                await self._enqueue_project_overview_refresh_best_effort(
                    project_ids=[overview_project_id],
                    actor_id=str(actor_id),
                    reason="scoped_memory_hard_deleted",
                )
                return {
                    "success": True,
                    "memory_id": memory_id,
                    "scope": before.get("scope_type"),
                    "operation": "hard_deleted",
                    "replaced_id": None,
                    "reason": reason,
                }
            memory.status = "forgotten"
            memory.version = int(memory.version or 1) + 1
            memory.updated_at = datetime.utcnow()
            self._add_audit(
                session,
                memory=memory,
                actor_id=str(actor_id),
                operation="forgotten",
                before=before,
                after=memory,
                turn_context=context,
                reason=reason,
            )
            await session.commit()
            await self._enqueue_project_overview_refresh_best_effort(
                project_ids=[overview_project_id],
                actor_id=str(actor_id),
                reason="scoped_memory_forgotten",
            )
            return self._result(memory, operation="forgotten", reason=reason)

    async def decide_candidate(
        self,
        memory_id: str,
        *,
        actor_id: str,
        approve: bool,
        expected_version: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        context = _turn_context(None, None)
        context.setdefault("user_id", str(actor_id))
        async with await self._new_session() as session:
            candidate = (
                await session.execute(
                    select(ContextMemory)
                    .where(ContextMemory.id == _uuid(memory_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if candidate is None:
                raise ScopedMemoryNotFound("memory not found")
            await self._require_scope_permission(
                session,
                scope=self._scope_from_memory(candidate),
                actor_id=str(actor_id),
                write=True,
            )
            if candidate.status != "candidate":
                raise ScopedMemoryConflict("memory is not a candidate")
            if int(candidate.version or 1) != int(expected_version):
                raise ScopedMemoryConflict("memory version changed")
            if not approve:
                before = _audit_snapshot(candidate)
                candidate.status = "rejected"
                candidate.rejection_reason = reason or "rejected_by_user"
                candidate.version = int(candidate.version or 1) + 1
                candidate.updated_at = datetime.utcnow()
                self._add_audit(
                    session,
                    memory=candidate,
                    actor_id=str(actor_id),
                    operation="rejected",
                    before=before,
                    after=candidate,
                    turn_context=context,
                    reason=candidate.rejection_reason,
                )
                await session.commit()
                return self._result(
                    candidate, operation="rejected", reason=candidate.rejection_reason
                )

            before = _audit_snapshot(candidate)
            candidate.status = "superseded"
            candidate.updated_at = datetime.utcnow()
            await session.flush()
            approved = ContextMemory(
                id=uuid.uuid4(),
                user_id=candidate.user_id,
                project_id=candidate.project_id,
                task_id=candidate.task_id,
                session_id=candidate.session_id,
                scope_type=candidate.scope_type,
                scope_id=candidate.scope_id,
                memory_type=candidate.memory_type,
                title=candidate.title,
                content=candidate.content,
                structured_data=candidate.structured_data or {},
                source_type="candidate_approval",
                source_ref=candidate.source_ref,
                confidence=candidate.confidence,
                importance=candidate.importance,
                trust_level="verified",
                sensitivity=candidate.sensitivity,
                evidence_refs=candidate.evidence_refs or [],
                evidence_span=candidate.evidence_span or {},
                dedupe_key=candidate.dedupe_key,
                supersedes_id=candidate.id,
                version=int(candidate.version or 1) + 1,
                created_by_actor=str(actor_id),
                projection_metadata=candidate.projection_metadata or {},
                status="active",
                is_pinned=candidate.is_pinned,
                expires_at=candidate.expires_at,
            )
            session.add(approved)
            await session.flush()
            self._add_audit(
                session,
                memory=approved,
                actor_id=str(actor_id),
                operation="approved",
                before=before,
                after=approved,
                turn_context=context,
                reason=reason or "approved_by_user",
            )
            overview_project_id = self._overview_active_project_id(approved)
            await session.commit()
            await session.refresh(approved)
            result = self._result(
                approved,
                operation="approved",
                reason=reason or "approved_by_user",
                replaced_id=candidate.id,
            )
        await self._enqueue_project_overview_refresh_best_effort(
            project_ids=[overview_project_id],
            actor_id=str(actor_id),
            reason="scoped_memory_candidate_approved",
        )
        return result

    async def record_correction(
        self,
        *,
        actor_id: str,
        subject: str,
        desired: str,
        evidence: str | None = None,
        utterance: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        clean_subject = str(subject or "").strip()
        clean_desired = str(desired or "").strip()
        if not clean_subject or not clean_desired:
            raise ScopedMemoryValidationError("correction subject and desired value are required")
        utterance_text = str(utterance or "")
        explicit_global = bool(_GLOBAL_CORRECTION_RE.search(utterance_text))
        explicit_task = bool(task_id and _TASK_CORRECTION_RE.search(utterance_text))
        explicit_session = bool(
            session_id and _SESSION_CORRECTION_RE.search(utterance_text)
        )
        if explicit_global:
            # "Every project" is still a preference owned by this user, not
            # a cross-tenant/global fact.
            scope_type, scope_id = "user", str(actor_id)
        elif explicit_session:
            scope_type, scope_id = "session", str(session_id)
        elif explicit_task:
            scope_type, scope_id = "task", str(task_id)
        elif project_id:
            scope_type, scope_id = "project", str(project_id)
        else:
            scope_type, scope_id = "user", str(actor_id)
        correction_key = _digest(_normalized_text(clean_subject))
        key = f"correction:{correction_key}"[:128]
        existing = await self.list_memories(
            actor_id=str(actor_id),
            scope_type=scope_type,
            scope_id=scope_id,
            project_id=project_id if scope_type == "project" else None,
            task_id=task_id if scope_type == "task" else None,
            session_id=session_id if scope_type == "session" else None,
            include_history=True,
        )
        matching = [
            item
            for item in existing
            if (item.get("structured_data") or {}).get("correction_key") == correction_key
            and _normalized_text((item.get("structured_data") or {}).get("desired"))
            == _normalized_text(clean_desired)
            and item.get("status") in {"candidate", "active"}
        ]
        active = next((item for item in matching if item.get("status") == "active"), None)
        if active:
            return {
                "success": True,
                "memory_id": active["id"],
                "scope": active["scope_type"],
                "scope_id": active["scope_id"],
                "operation": "unchanged",
                "replaced_id": None,
                "reason": "correction_already_active",
                "memory": active,
            }
        candidate = next((item for item in matching if item.get("status") == "candidate"), None)
        if candidate:
            return await self.decide_candidate(
                candidate["id"],
                actor_id=str(actor_id),
                approve=True,
                expected_version=int(candidate.get("version") or 1),
                reason="identical_correction_confirmed_twice",
            )
        return await self.upsert_memory(
            actor_id=str(actor_id),
            content=f"{clean_subject}: {clean_desired}",
            scope_type=scope_type,
            scope_id=scope_id,
            project_id=project_id if scope_type == "project" else None,
            task_id=task_id if scope_type == "task" else None,
            session_id=session_id if scope_type == "session" else None,
            memory_type="correction",
            structured_data={
                "correction_key": correction_key,
                "subject": clean_subject,
                "desired": clean_desired,
                "evidence": evidence,
                "explicit_global": explicit_global,
            },
            source_type="correction",
            evidence_refs=[
                {"type": "user_correction", "value": evidence or utterance or clean_desired}
            ],
            dedupe_key=key,
            status="candidate",
            trust_level="verified",
            importance=8,
        )

    async def move_scope(
        self,
        memory_id: str,
        *,
        actor_id: str,
        expected_version: int,
        scope_type: str,
        scope_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        reason: str = "scope_move",
        turn_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        target_scope = _scope(
            actor_id=str(actor_id),
            scope_type=scope_type,
            scope_id=scope_id,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
        )
        context = _turn_context(turn_context, None)
        context.setdefault("user_id", str(actor_id))
        move_reason = reason or f"moved_to_{target_scope.scope_type}_scope"
        async with await self._new_session() as session:
            source = (
                await session.execute(
                    select(ContextMemory)
                    .where(ContextMemory.id == _uuid(memory_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if source is None:
                raise ScopedMemoryNotFound("memory not found")
            await self._require_scope_permission(
                session,
                scope=self._scope_from_memory(source),
                actor_id=str(actor_id),
                write=True,
            )
            await self._require_scope_permission(
                session,
                scope=target_scope,
                actor_id=str(actor_id),
                write=True,
            )
            if int(source.version or 1) != int(expected_version):
                raise ScopedMemoryConflict("memory version changed")
            if source.status not in ACTIVE_STATUSES:
                raise ScopedMemoryConflict(
                    f"memory cannot move in status {source.status}"
                )
            source_overview_project_id = self._overview_active_project_id(source)
            if (
                source.scope_type == target_scope.scope_type
                and str(source.scope_id or "") == target_scope.scope_id
            ):
                raise ScopedMemoryValidationError(
                    "target scope must differ from the current scope"
                )

            key = source.dedupe_key or _dedupe_key(
                source.content, source.memory_type
            )
            target_rows = list(
                (
                    await session.execute(
                        select(ContextMemory)
                        .where(
                            ContextMemory.user_id == str(actor_id),
                            ContextMemory.scope_type == target_scope.scope_type,
                            ContextMemory.scope_id == target_scope.scope_id,
                            ContextMemory.dedupe_key == key,
                            ContextMemory.status.in_(tuple(ACTIVE_STATUSES)),
                        )
                        .order_by(ContextMemory.version.desc())
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            source_before = _audit_snapshot(source)
            target_before = [(_audit_snapshot(row), row) for row in target_rows]
            source_status = source.status
            now = datetime.utcnow()
            source.status = "superseded"
            source.updated_at = now
            for row in target_rows:
                row.status = "superseded"
                row.updated_at = now
            await session.flush()

            predecessor = target_rows[0] if target_rows else source
            moved_status = (
                "active"
                if source_status == "active"
                or any(before.get("status") == "active" for before, _ in target_before)
                else "candidate"
            )
            metadata = dict(source.projection_metadata or {})
            metadata.update(
                {
                    "idempotency_key": (
                        f"scope-move:{source.id}:{expected_version}:"
                        f"{target_scope.scope_type}:{target_scope.scope_id}"
                    ),
                    "scope_move": {
                        "source_memory_id": str(source.id),
                        "source_scope": source.scope_type,
                        "target_scope": target_scope.scope_type,
                        "target_scope_id": target_scope.scope_id,
                        "reason": move_reason,
                    },
                    "turn_context": context,
                }
            )
            moved = ContextMemory(
                id=uuid.uuid4(),
                user_id=str(actor_id),
                project_id=target_scope.project_id,
                task_id=target_scope.task_id,
                session_id=target_scope.session_id,
                scope_type=target_scope.scope_type,
                scope_id=target_scope.scope_id,
                memory_type=source.memory_type,
                title=source.title,
                content=source.content,
                structured_data=dict(source.structured_data or {}),
                source_type="scope_move",
                source_ref=f"context_memory:{source.id}",
                confidence=source.confidence,
                importance=source.importance,
                trust_level=source.trust_level,
                sensitivity=source.sensitivity,
                evidence_refs=[
                    *(source.evidence_refs or []),
                    {
                        "type": "scope_move",
                        "memory_id": str(source.id),
                        "reason": move_reason,
                    },
                ],
                evidence_span=dict(source.evidence_span or {}),
                dedupe_key=key,
                supersedes_id=predecessor.id,
                version=max(
                    [int(source.version or 1)]
                    + [int(row.version or 1) for row in target_rows]
                )
                + 1,
                created_by_actor=str(actor_id),
                rejection_reason=source.rejection_reason,
                projection_metadata=metadata,
                migration_id=source.migration_id,
                status=moved_status,
                is_pinned=source.is_pinned,
                expires_at=source.expires_at,
                created_at=now,
                updated_at=now,
            )
            session.add(moved)
            await session.flush()
            self._add_audit(
                session,
                memory=moved,
                actor_id=str(actor_id),
                operation="moved",
                before=source_before,
                after=moved,
                turn_context=context,
                reason=move_reason,
            )
            for before, row in target_before:
                self._add_audit(
                    session,
                    memory=row,
                    actor_id=str(actor_id),
                    operation="superseded_by_scope_move",
                    before=before,
                    after=row,
                    turn_context=context,
                    reason=move_reason,
                )
            await session.commit()
            await session.refresh(moved)
            result = self._result(
                moved,
                operation="moved",
                reason=move_reason,
                replaced_id=source.id,
            )
            target_overview_project_id = self._overview_active_project_id(moved)
        await self._enqueue_project_overview_refresh_best_effort(
            project_ids=[
                source_overview_project_id,
                target_overview_project_id,
            ],
            actor_id=str(actor_id),
            reason="scoped_memory_scope_moved",
        )
        return result

    async def retrieve_for_context(
        self,
        *,
        actor_id: str,
        project_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        query: str = "",
        limit: int = 20,
        max_chars: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        actor_id = _canonical_actor_id(actor_id)
        project_uuid = _uuid(project_id)
        task_uuid = _uuid(task_id)
        session_uuid = _uuid(session_id)
        # A malformed optional scope must never be silently dropped and
        # replaced by broader user/global context.  Callers can intentionally
        # omit a scope with None/"", but a non-empty invalid identifier is a
        # validation failure (the advisory retrieval path follows the same
        # fail-closed contract).
        if project_id not in (None, "") and project_uuid is None:
            raise ScopedMemoryValidationError("project_id must be a UUID")
        if task_id not in (None, "") and task_uuid is None:
            raise ScopedMemoryValidationError("task_id must be a UUID")
        if session_id not in (None, "") and session_uuid is None:
            raise ScopedMemoryValidationError("session_id must be a UUID")
        conditions = [
            and_(ContextMemory.scope_type == "global", ContextMemory.user_id == str(actor_id)),
            and_(ContextMemory.scope_type == "user", ContextMemory.user_id == str(actor_id)),
        ]
        if project_uuid:
            conditions.append(
                and_(
                    ContextMemory.scope_type == "project",
                    ContextMemory.project_id == project_uuid,
                    ContextMemory.scope_id == str(project_uuid),
                )
            )
        if task_uuid:
            conditions.append(and_(ContextMemory.scope_type == "task", ContextMemory.task_id == task_uuid))
        if session_uuid:
            conditions.append(
                and_(ContextMemory.scope_type == "session", ContextMemory.session_id == session_uuid)
            )
        async with await self._new_session() as session:
            if project_uuid:
                await self._require_project_permission(
                    session,
                    project_id=project_uuid,
                    actor_id=str(actor_id),
                    write=False,
                )
            if task_uuid:
                await self._require_scope_permission(
                    session,
                    scope=_scope(
                        actor_id=str(actor_id),
                        scope_type="task",
                        task_id=str(task_uuid),
                    ),
                    actor_id=str(actor_id),
                    write=False,
                    expected_project_id=project_uuid,
                )
            if session_uuid:
                await self._require_scope_permission(
                    session,
                    scope=_scope(
                        actor_id=str(actor_id),
                        scope_type="session",
                        session_id=str(session_uuid),
                    ),
                    actor_id=str(actor_id),
                    write=False,
                    expected_project_id=project_uuid,
                )
            rows = list(
                (
                    await session.execute(
                        select(ContextMemory)
                        .where(
                            ContextMemory.status == "active",
                            or_(ContextMemory.expires_at.is_(None), ContextMemory.expires_at > datetime.utcnow()),
                            or_(*conditions),
                        )
                        .order_by(ContextMemory.updated_at.desc())
                        .limit(max(200, int(limit) * 20))
                    )
                )
                .scalars()
                .all()
            )

        terms = _keywords(query)
        now = datetime.utcnow()
        scored: list[tuple[float, ContextMemory, str]] = []
        trace: list[dict[str, Any]] = []
        for row in rows:
            row_terms = _keywords(f"{row.title or ''}\n{row.content or ''}")
            keyword_overlap = len(terms & row_terms) / max(1, len(terms)) if terms else 0.0
            scope_score = SCOPE_PRIORITY.get(row.scope_type, 0) / 5
            importance_score = max(0, min(int(row.importance or 0), 10)) / 10
            trust_score = {"verified": 1.0, "trusted": 0.9, "inferred": 0.6, "unverified": 0.3}.get(
                str(row.trust_level or "inferred"), 0.5
            )
            age_days = max(0.0, (now - (row.updated_at or row.created_at or now)).total_seconds() / 86400)
            recency_score = 1.0 / (1.0 + age_days / 30)
            score = (
                keyword_overlap * 0.28
                + scope_score * 0.25
                + importance_score * 0.18
                + trust_score * 0.12
                + recency_score * 0.07
                + (0.10 if row.is_pinned else 0.0)
            )
            reason = (
                "pinned"
                if row.is_pinned
                else "keyword_and_scope_match"
                if keyword_overlap
                else "scope_priority"
            )
            scored.append((score, row, reason))
        scored.sort(key=lambda item: (item[0], item[1].importance), reverse=True)
        selected: list[dict[str, Any]] = []
        used_chars = 0
        for score, row, reason in scored:
            data = row.to_dict()
            estimated_chars = len(str(data.get("content") or "")) + 64
            included = len(selected) < int(limit) and (
                max_chars is None or used_chars + estimated_chars <= max_chars
            )
            trace.append(
                {
                    "memory_id": str(row.id),
                    "scope": row.scope_type,
                    "reason": reason if included else "context_budget_exceeded",
                    "score": round(score, 4),
                    "source": row.source_type,
                    "excluded": not included,
                    "cost_chars": estimated_chars,
                }
            )
            if not included:
                continue
            data["retrieval_score"] = round(score, 4)
            data["selection_reason"] = reason
            selected.append(data)
            used_chars += estimated_chars
        return selected, trace

    async def retrieve_advisory_conflict_candidates(
        self,
        *,
        actor_id: str,
        project_id: str,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 64,
    ) -> list[dict[str, Any]]:
        """Return a bounded, read-only Memory projection for WI conflicts.

        This query is deliberately separate from ``retrieve_for_context``:
        prompt retrieval keeps its relevance and character budgets, while the
        Work Intelligence compiler may inspect a wider recency window solely
        to mark advisory conflicts.  Identifiers, source metadata, timestamps,
        and evidence never cross this transient projection boundary.
        """

        actor_id = _canonical_actor_id(actor_id)
        project_uuid = _uuid(project_id)
        if project_uuid is None:
            raise ScopedMemoryValidationError("project_id is required")
        task_uuid = _uuid(task_id)
        session_uuid = _uuid(session_id)
        if task_id not in (None, "") and task_uuid is None:
            raise ScopedMemoryValidationError("task_id must be a UUID")
        if session_id not in (None, "") and session_uuid is None:
            raise ScopedMemoryValidationError("session_id must be a UUID")
        bounded_limit = max(1, min(int(limit or 64), 64))

        conditions = [
            and_(
                ContextMemory.scope_type == "project",
                ContextMemory.project_id == project_uuid,
                ContextMemory.scope_id == str(project_uuid),
            )
        ]
        if task_uuid is not None:
            conditions.append(
                and_(
                    ContextMemory.scope_type == "task",
                    ContextMemory.task_id == task_uuid,
                )
            )
        if session_uuid is not None:
            conditions.append(
                and_(
                    ContextMemory.scope_type == "session",
                    ContextMemory.session_id == session_uuid,
                )
            )

        async with await self._new_session() as session:
            await self._require_project_permission(
                session,
                project_id=project_uuid,
                actor_id=str(actor_id),
                write=False,
            )
            if task_uuid is not None:
                task_scope = _scope(
                    actor_id=str(actor_id),
                    scope_type="task",
                    task_id=str(task_uuid),
                )
                await self._require_scope_permission(
                    session,
                    scope=task_scope,
                    actor_id=str(actor_id),
                    write=False,
                )
                task = await session.get(Task, task_uuid)
                if (
                    task is None
                    or task.deleted_at is not None
                    or not _same_uuid(task.project_id, project_uuid)
                ):
                    raise ScopedMemoryPermissionDenied("task is outside project scope")
            if session_uuid is not None:
                session_scope = _scope(
                    actor_id=str(actor_id),
                    scope_type="session",
                    session_id=str(session_uuid),
                )
                await self._require_scope_permission(
                    session,
                    scope=session_scope,
                    actor_id=str(actor_id),
                    write=False,
                )
                conversation = await session.get(ConversationSession, session_uuid)
                if (
                    conversation is None
                    or conversation.deleted_at is not None
                    or not _same_uuid(conversation.project_id, project_uuid)
                ):
                    raise ScopedMemoryPermissionDenied(
                        "session is outside project scope"
                    )

            rows = list(
                (
                    await session.execute(
                        select(ContextMemory)
                        .where(
                            ContextMemory.status == "active",
                            or_(
                                ContextMemory.expires_at.is_(None),
                                ContextMemory.expires_at > datetime.utcnow(),
                            ),
                            or_(*conditions),
                        )
                        .order_by(ContextMemory.updated_at.desc())
                        .limit(bounded_limit)
                    )
                )
                .scalars()
                .all()
            )

        return [
            {
                "scope_type": str(row.scope_type or ""),
                "title": row.title,
                "content": str(row.content or ""),
                "structured_data": (
                    dict(row.structured_data)
                    if isinstance(row.structured_data, dict)
                    else {}
                ),
            }
            for row in rows
        ]

    async def search(
        self,
        *,
        actor_id: str,
        query: str,
        project_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        actor_id = _canonical_actor_id(actor_id)
        selected, _ = await self.retrieve_for_context(
            actor_id=str(actor_id),
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            query=query,
            limit=limit,
        )
        return selected

    async def explain(self, memory_id: str, *, actor_id: str) -> dict[str, Any]:
        actor_id = _canonical_actor_id(actor_id)
        async with await self._new_session() as session:
            memory = await session.get(ContextMemory, _uuid(memory_id))
            if memory is None:
                raise ScopedMemoryNotFound("memory not found")
            await self._require_scope_permission(
                session,
                scope=self._scope_from_memory(memory),
                actor_id=str(actor_id),
                write=False,
            )
            ancestors: list[dict[str, Any]] = []
            current = memory
            seen: set[uuid.UUID] = set()
            while current.supersedes_id and current.supersedes_id not in seen:
                seen.add(current.supersedes_id)
                current = await session.get(ContextMemory, current.supersedes_id)
                if current is None:
                    break
                ancestors.append(current.to_dict())
            descendants = list(
                (
                    await session.execute(
                        select(ContextMemory).where(ContextMemory.supersedes_id == memory.id)
                    )
                )
                .scalars()
                .all()
            )
            return {
                "memory": memory.to_dict(),
                "lineage": {
                    "ancestors": ancestors,
                    "descendants": [item.to_dict() for item in descendants],
                },
                "explanation": {
                    "scope_priority": SCOPE_PRIORITY.get(memory.scope_type, 0),
                    "trust_level": memory.trust_level,
                    "source_type": memory.source_type,
                    "evidence_refs": memory.evidence_refs or [],
                    "dedupe_key": memory.dedupe_key,
                },
            }

    async def promote_to_project_information(
        self,
        memory_id: str,
        *,
        actor_id: str,
        expected_version: int,
        target_section: str | None = None,
        source_refs: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Explicitly materialize one memory as an editable Docs block.

        Scoped Memory provenance is retained in the child block's
        ``body_json.clip_ingest`` metadata.  The project information topic is
        only a container; it never receives a ``verbatim_blocks`` payload.
        """
        actor_id = _canonical_actor_id(actor_id)
        from ..memory.models import KnowledgeNode
        from .clip_ingest_service import ClipIngestService
        from .docs_graph_service import DocsGraphService
        from .docs_workspace import get_canonical_project_information_node

        context = _turn_context(None, None)
        context.setdefault("user_id", str(actor_id))
        async with await self._new_session() as session:
            memory = (
                await session.execute(
                    select(ContextMemory)
                    .where(ContextMemory.id == _uuid(memory_id))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if memory is None:
                raise ScopedMemoryNotFound("memory not found")
            if memory.project_id is None:
                raise ScopedMemoryValidationError(
                    "promotion requires a project-scoped memory"
                )
            await self._require_project_permission(
                session,
                project_id=memory.project_id,
                actor_id=str(actor_id),
                write=True,
            )
            if int(memory.version or 1) != int(expected_version):
                raise ScopedMemoryConflict("memory version changed")
            overview_project_id = self._overview_active_project_id(memory)
            project = await session.get(Project, memory.project_id)
            node = await get_canonical_project_information_node(
                session,
                project_id=memory.project_id,
                actor_user_id=_uuid(actor_id),
            )
            if project is None or node is None:
                raise ScopedMemoryNotFound("canonical Project Information page not found")

            content = str(memory.content or "").strip()
            block = ClipIngestService._make_verbatim_block(
                source_id=str(memory.id),
                source={
                    "source_type": "scoped_memory",
                    "url": f"memory://{memory.id}",
                },
                start_line=1,
                end_line=max(1, content.count("\n") + 1),
                kind="quote",
                label=target_section or memory.title or "Scoped Memory",
                content=content,
            )
            promotion_source_refs = [
                {
                    "type": "context_memory",
                    "memory_id": str(memory.id),
                    "version": memory.version,
                    "scope": memory.scope_type,
                },
                *[
                    dict(item)
                    for item in source_refs or []
                    if isinstance(item, dict)
                ],
            ]

            # A previous implementation stored the block on the topic.  Do
            # not use that legacy key as the user-visible source of truth, but
            # recognize an identical typed child for retry idempotence.  The
            # query is best-effort for lightweight adapters; the real
            # DocsGraphService write below remains authoritative.
            existing_children: list[KnowledgeNode] = []
            try:
                children_result = await session.execute(
                    select(KnowledgeNode).where(
                        KnowledgeNode.parent_id == node.id,
                        KnowledgeNode.docs_library_id == node.docs_library_id,
                        KnowledgeNode.archived_at.is_(None),
                    )
                )
                existing_children = [
                    child
                    for child in children_result.scalars().all()
                    if getattr(child, "parent_id", None) == node.id
                    and getattr(child, "archived_at", None) is None
                ]
            except Exception:
                existing_children = []
            for child in existing_children:
                child_body = getattr(child, "body_json", None)
                child_metadata = (
                    child_body.get("clip_ingest")
                    if isinstance(child_body, dict)
                    else None
                )
                if (
                    isinstance(child_body, dict)
                    and child_body.get("format") == "doc_block"
                    and isinstance(child_metadata, dict)
                    and child_metadata.get("source_id") == str(memory.id)
                    and child_metadata.get("sha256") == block["sha256"]
                    and child_body.get("content") == content
                ):
                    return {
                        "success": True,
                        "memory_id": str(memory.id),
                        "scope": memory.scope_type,
                        "operation": "unchanged",
                        "replaced_id": None,
                        "reason": "already_promoted",
                        "project_information_node_id": str(node.id),
                        "project_information_block_node_id": str(child.id),
                    }

            body_json = ClipIngestService._typed_block_body_json(block)
            block_label = body_json["label"]
            block_node = await DocsGraphService(session).create_node(
                docs_library_id=node.docs_library_id,
                user_id=_uuid(actor_id),
                title=block_label,
                parent=node,
                project_id=node.project_id,
                body_json=body_json,
                source_refs=promotion_source_refs,
            )
            before = _audit_snapshot(memory)
            metadata = dict(memory.projection_metadata or {})
            metadata["project_information_projection"] = {
                "node_id": str(node.id),
                "block_node_id": str(block_node.id),
                "content_sha256": block["sha256"],
                "promoted_at": datetime.utcnow().isoformat(),
                "promoted_by": str(actor_id),
                "target_section": target_section,
                "source_refs": promotion_source_refs,
            }
            memory.projection_metadata = metadata
            memory.version = int(memory.version or 1) + 1
            memory.updated_at = datetime.utcnow()
            self._add_audit(
                session,
                memory=memory,
                actor_id=str(actor_id),
                operation="promoted_to_project_information",
                before=before,
                after=memory,
                turn_context=context,
                reason="explicit_user_promotion",
            )
            await session.commit()
            result = {
                "success": True,
                "memory_id": str(memory.id),
                "scope": memory.scope_type,
                "operation": "promoted",
                "replaced_id": None,
                "reason": "explicit_user_promotion",
                "project_information_node_id": str(node.id),
                "project_information_block_node_id": str(block_node.id),
            }
        await self._enqueue_project_overview_refresh_best_effort(
            project_ids=[overview_project_id],
            actor_id=str(actor_id),
            reason="scoped_memory_promoted_to_project_information",
        )
        return result

    @staticmethod
    def render_memories_for_prompt(memories: list[dict[str, Any]]) -> str:
        if not memories:
            return ""
        labels = {
            "global": "Global Memory",
            "user": "User Memory",
            "project": "Project Memory",
            "task": "Task Memory",
            "session": "Session Memory",
        }
        lines: list[str] = []
        for scope_type in ("user", "project", "task", "session", "global"):
            scoped = [item for item in memories if item.get("scope_type") == scope_type]
            if not scoped:
                continue
            lines.append(f"## {labels[scope_type]}")
            for item in scoped:
                title = f"{item.get('title')}: " if item.get("title") else ""
                lines.append(
                    f"- {title}{item.get('content')} "
                    f"[trust={item.get('trust_level')}, reason={item.get('selection_reason')}]"
                )
        return "\n".join(lines)


def parse_correction_utterance(text: str) -> dict[str, Any] | None:
    """Best-effort Japanese/English correction parsing for tool prefill."""
    raw = str(text or "").strip()
    marker = _CORRECTION_MARKER_RE.search(raw)
    if not marker:
        return None
    before = raw[: marker.start()].strip(" 、。,:：")
    after = raw[marker.end() :].strip(" 、。,:：")
    if not after:
        after = "直前の回答を採用せず、会話中の最新のユーザー指示を優先する"
    return {
        "subject": before or "ユーザー指定事項",
        "desired": after,
        "explicit_global": bool(_GLOBAL_CORRECTION_RE.search(raw)),
    }


__all__ = [
    "MemoryScope",
    "ScopedMemoryConflict",
    "ScopedMemoryError",
    "ScopedMemoryNotFound",
    "ScopedMemoryPermissionDenied",
    "ScopedMemoryService",
    "ScopedMemoryValidationError",
    "_canonical_project_evidence_id",
    "canonical_evidence_identity",
    "canonical_project_memory_evidence_id",
    "classify_sensitivity",
    "parse_correction_utterance",
    "project_memory_evidence_identity",
    "project_memory_evidence_ids",
    "project_memory_semantic_identity",
]
