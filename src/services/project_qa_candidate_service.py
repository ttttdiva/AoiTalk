"""Create project Q&A candidates from saved chat turns."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import unicodedata
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, or_, select

from ..memory.database import get_db_session
from ..memory.models import (
    AgentRun,
    AgentRunToolCall,
    ConversationMessage,
    ConversationSession,
    Project,
    ProjectMember,
    ProjectQaEntry,
    User,
)
from .project_permissions import has_effective_project_permission
from .project_information_docs import ensure_project_information_doc
from .privacy_masking_projection import is_privacy_masking_source
from .scoped_memory_service import classify_sensitivity_fields

logger = logging.getLogger(__name__)

_QUESTION_HINT_RE = re.compile(
    r"(?:\?|？|何|いつ|どこ|誰|どれ|どの|どう|なぜ|教えて|確認したい|必要ですか|ありますか|できますか|ですか|ますか|でしょうか)"
)


_SHORT_ASCII_FRAGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-\s]{0,15}[?？]$")
_ASCII_QUESTION_WORD_RE = re.compile(
    r"\b(?:who|what|when|where|why|how|which|can|could|should|would|do|does|did|is|are|will)\b",
    re.IGNORECASE,
)


def _is_noise_question_candidate(question: str) -> bool:
    normalized = re.sub(r"\s+", " ", question.strip())
    core = normalized.strip(" \t?？!！.。")
    if len(core) < 4:
        return True
    if (
        _SHORT_ASCII_FRAGMENT_RE.fullmatch(normalized)
        and not _ASCII_QUESTION_WORD_RE.search(normalized)
    ):
        return True
    return False


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _normalized_question_hash(value: str) -> str:
    normalized = " ".join(str(value or "").strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _clean_semantic_text(value: Any, *, limit: int) -> str:
    """Return bounded text for a provider-generated semantic artifact."""

    if value in (None, ""):
        return ""
    # Do not coerce mappings/lists into repr strings.  A provider payload is
    # untrusted and only scalar text is meaningful for a Q&A entry.
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").replace("\r\n", "\n").strip()[:limit]


def _artifact_type(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def is_project_qa_artifact(value: Any) -> bool:
    """Return whether extractor output explicitly declares a semantic Q&A."""

    if not isinstance(value, dict):
        return False
    marker = (
        value.get("artifact_type")
        or value.get("candidate_type")
        or value.get("kind")
        or value.get("type")
    )
    normalized = _artifact_type(marker)
    if normalized in PROJECT_QA_ARTIFACT_TYPES:
        return True
    # ``scope_intent`` is accepted as a compatibility alias only when the
    # provider explicitly opts into the Q&A artifact.  A normal ``project``
    # memory must never be interpreted as a question merely because it has
    # question-like punctuation.
    return _artifact_type(value.get("scope_intent")) in {
        "project-qa",
        "qa",
        "question-answer",
    }


def normalize_project_qa_candidate(
    value: Any,
    *,
    user_input: str,
    assistant_response: str,
) -> dict[str, Any] | None:
    """Validate one provider-generated, semantic Project Q&A candidate.

    The server never extracts a question from raw turn text.  A candidate is
    accepted only when the extractor marks it as ``project_qa`` and supplies a
    bounded rewritten question plus an exact user evidence span.  Answers are
    retained only when the provider says that the assistant actually supports
    them and the evidence span belongs to the assistant response.
    """

    if not is_project_qa_artifact(value):
        return None
    if not isinstance(value, dict):
        return None
    action = str(value.get("action") or "upsert").strip().lower()
    if action not in {"upsert", "update"}:
        # Q&A deletion/rejection remains an explicit UI/tool operation; an
        # inferred extraction item must not mutate an existing row.
        return None

    question = _clean_semantic_text(
        value.get("question") or value.get("title"),
        limit=PROJECT_QA_MAX_QUESTION_LENGTH,
    )
    if len(question) < 8:
        return None
    evidence_span = _clean_semantic_text(
        value.get("evidence_span") or value.get("question_evidence_span"),
        limit=PROJECT_QA_MAX_EVIDENCE_LENGTH,
    )
    raw_user_input = str(user_input or "")
    if len(evidence_span) < 4 or evidence_span not in raw_user_input:
        # Exact evidence is the containment boundary.  We intentionally do
        # not attempt fuzzy/regex matching here because that would recreate
        # the old raw-fragment ingestion path.
        return None

    answer_supported = value.get("answer_supported") is True or value.get(
        "answer_evidence_supported"
    ) is True
    answer = _clean_semantic_text(
        value.get("answer"),
        limit=PROJECT_QA_MAX_ANSWER_LENGTH,
    )
    answer_evidence_span = _clean_semantic_text(
        value.get("answer_evidence_span") or value.get("assistant_evidence_span"),
        limit=PROJECT_QA_MAX_EVIDENCE_LENGTH,
    )
    if not answer_supported:
        answer = ""
        answer_evidence_span = ""
    elif not answer or not answer_evidence_span or answer_evidence_span not in str(
        assistant_response or ""
    ):
        # Never persist an answer that cannot be tied to the completed
        # assistant turn.  Keep the question as an unanswered candidate only
        # when the provider omitted/invalidated answer support.
        answer = ""
        answer_evidence_span = ""
        answer_supported = False

    # Quality fields are part of the provider-output contract.  Do not coerce
    # NaN/Infinity, booleans, or fractional values into a plausible score:
    # such coercion can surface malformed model output as reviewable project
    # knowledge.  The omitted-confidence compatibility default is retained
    # for legacy extractors; an explicitly malformed or out-of-range value is
    # rejected.
    if "confidence" not in value:
        confidence = PROJECT_QA_MIN_CONFIDENCE
    else:
        raw_confidence = value.get("confidence")
        if isinstance(raw_confidence, bool) or not isinstance(
            raw_confidence, (int, float)
        ):
            return None
        confidence = float(raw_confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            return None
    if confidence < PROJECT_QA_MIN_CONFIDENCE:
        return None

    raw_importance = value.get("importance", 1)
    if isinstance(raw_importance, bool) or not isinstance(raw_importance, (int, float)):
        return None
    if isinstance(raw_importance, float) and (
        not math.isfinite(raw_importance) or not raw_importance.is_integer()
    ):
        return None
    importance = int(raw_importance)
    if not 1 <= importance <= 10:
        return None
    normalized: dict[str, Any] = {
        "artifact_type": "project_qa",
        "action": action,
        "question": question,
        "answer": answer or None,
        "answer_supported": bool(answer_supported),
        "confidence": confidence,
        "importance": importance,
        "evidence_span": evidence_span,
    }
    if answer_evidence_span:
        normalized["answer_evidence_span"] = answer_evidence_span
    return normalized


def _source_job_refs(entry: Any) -> list[dict[str, Any]]:
    raw = getattr(entry, "answer_source_refs", None)
    return [item for item in (raw or []) if isinstance(item, dict)]


def _entry_has_source_job(entry: Any, job_id: str) -> bool:
    normalized = str(job_id or "").strip()
    if not normalized:
        return False
    normalized_uuid = _coerce_uuid(normalized)
    for reference in _source_job_refs(entry):
        for key in ("source_job_id", "job_id", "id"):
            value = str(reference.get(key) or "").strip()
            same_job = value == normalized or (
                normalized_uuid is not None and _coerce_uuid(value) == normalized_uuid
            )
            if same_job and str(reference.get("type") or "").strip().lower() in {
                "scoped_memory_job",
                "project_qa_candidate",
                "memory_job",
                "scoped-memory-job",
            }:
                return True
    # Some historical rows carry provenance in source_message_ids as a
    # structured object.  Read it defensively without treating plain message
    # IDs as job IDs.
    raw_sources = getattr(entry, "source_message_ids", None)
    for source in raw_sources or []:
        if isinstance(source, dict):
            source_job = str(source.get("job_id") or "").strip()
            if source_job == normalized or (
                normalized_uuid is not None and _coerce_uuid(source_job) == normalized_uuid
            ):
                return True
    return False


def _append_source_ref(entry: Any, ref: dict[str, Any]) -> None:
    refs = _source_job_refs(entry)
    if ref not in refs:
        refs.append(ref)
    try:
        entry.answer_source_refs = refs[:32]
    except Exception:
        pass


def _append_string_ref(entry: Any, field: str, value: Any, *, limit: int = 32) -> None:
    """Append a bounded scalar provenance id to one of the JSON list fields."""

    normalized = str(value or "").strip()
    if not normalized:
        return
    values = list(getattr(entry, field, None) or [])
    if normalized not in values:
        values.append(normalized)
    try:
        setattr(entry, field, values[:limit])
    except Exception:
        pass


_CLOSED_QA_STATUSES = frozenset({"answered", "resolved", "stale", "cancelled", "archived"})

# Provider output is deliberately kept behind a closed, small schema.  The
# semantic extractor is allowed to emit at most three project questions for a
# turn; malformed/ambiguous output is discarded rather than downgraded to a
# raw-text candidate.
PROJECT_QA_ARTIFACT_TYPES = frozenset(
    {"project-qa", "project-q-a", "qa", "question-answer"}
)
PROJECT_QA_MAX_CANDIDATES = 3
PROJECT_QA_MIN_CONFIDENCE = 0.6
PROJECT_QA_MAX_QUESTION_LENGTH = 500
PROJECT_QA_MAX_ANSWER_LENGTH = 2000
PROJECT_QA_MAX_EVIDENCE_LENGTH = 500
_DOCS_MUTATION_TOOL_NAMES = frozenset(
    {
        "docs_create_nodes",
        "docs_update_node",
        "docs_ensure_inbox",
        "docs_attach_workspace_file",
        "docs_place_workspace_file",
        "docs_move_node",
        "docs_archive_node",
        "patch_project_information_doc",
        "inbox_update_item",
    }
)


def is_project_qa_entry_closed(entry: ProjectQaEntry) -> bool:
    """Return whether an automatic intake must leave this canonical row untouched."""
    return (
        getattr(entry, "deleted_at", None) is not None
        or str(getattr(entry, "status", "") or "").strip().lower()
        in _CLOSED_QA_STATUSES
        or str(getattr(entry, "review_state", "") or "").strip().lower()
        in {"rejected", "accepted"}
        or str(getattr(entry, "origin", "") or "").strip().lower() == "manual"
    )


def _normal_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\r\n", "\n").split()).casefold()


def _same_actor(value: Any, expected: Any) -> bool:
    left = _coerce_uuid(value)
    right = _coerce_uuid(expected)
    if left is not None and right is not None:
        return left == right
    return str(value or "").strip().casefold() == str(expected or "").strip().casefold()


def _normalized_chat_text(value: Any) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "").replace("\r\n", "\n"))
        .strip()
        .split()
    )


async def _validate_source_chat_binding(
    session: Any,
    *,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    session_id: uuid.UUID,
    source_message_id: uuid.UUID | None,
    user_input: str,
) -> bool:
    """Hold the source chat rows while a semantic QA candidate is written.

    Production AsyncSession takes row locks for both the ConversationSession
    and its optional source message.  A minimal legacy test double may expose
    only ``get``; when no message provenance is available it cannot model a
    lock, so the already-validated job binding remains the compatibility
    boundary for that adapter.
    """

    execute = getattr(session, "execute", None)
    if not callable(execute) and source_message_id is None:
        return True

    async def locked(model: Any, identifier: uuid.UUID) -> Any:
        if callable(execute):
            result = await execute(
                select(model)
                .where(model.id == identifier)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            scalar_one_or_none = getattr(result, "scalar_one_or_none", None)
            if callable(scalar_one_or_none):
                return scalar_one_or_none()
            one_or_none = getattr(result, "one_or_none", None)
            if callable(one_or_none):
                row = one_or_none()
                if isinstance(row, tuple):
                    return row[0] if row else None
                return row
            scalars = getattr(result, "scalars", None)
            if callable(scalars):
                values = scalars()
                first = getattr(values, "first", None)
                if callable(first):
                    return first()
            return None
        return await session.get(model, identifier)

    # Project lifecycle writers lock Project before bulk-updating linked chat
    # rows.  Acquire that same parent lock before ConversationSession and
    # ConversationMessage so completed-turn Q&A curation cannot form a
    # Session -> Project versus Project -> Session deadlock during deletion or
    # rebinding.
    project = await locked(Project, project_id)
    if (
        project is None
        or getattr(project, "deleted_at", None) is not None
        or bool(getattr(project, "is_completed", False))
    ):
        return False

    conversation = await locked(ConversationSession, session_id)
    if conversation is None or getattr(conversation, "deleted_at", None) is not None:
        return False
    if not _same_actor(getattr(conversation, "user_id", None), actor_id):
        return False
    if not _same_actor(getattr(conversation, "project_id", None), project_id):
        return False

    if source_message_id is None:
        return True
    message = await locked(ConversationMessage, source_message_id)
    if message is None or getattr(message, "deleted_at", None) is not None:
        return False
    if is_privacy_masking_source(message):
        return False
    if not _same_actor(getattr(message, "session_id", None), session_id):
        return False
    if str(getattr(message, "role", "") or "").strip().casefold() != "user":
        return False
    if not _same_actor(getattr(message, "sender_id", None), actor_id):
        return False
    if user_input not in (None, "") and _normalized_chat_text(
        getattr(message, "content", None)
    ) != _normalized_chat_text(user_input):
        return False
    return True


async def _load_project_for_update(session: Any, project_id: uuid.UUID) -> Any:
    """Reload and row-lock a Project when the session supports SQLAlchemy execute.

    Small unit-test doubles and rolling deployments may expose only ``get``;
    production AsyncSession always takes the locked SELECT path.  Keeping the
    compatibility fallback local avoids weakening the real transaction while
    preserving the service's optional-schema test surface.
    """

    execute = getattr(session, "execute", None)
    if callable(execute):
        result = await execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
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
    return await session.get(Project, project_id)


def _iter_text_values(value: Any, *, _depth: int = 0):
    """Yield scalar strings from a bounded tool receipt payload."""

    if _depth > 4:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in list(value.items())[:64]:
            # IDs/statuses are useful correlation data but cannot prove that
            # a Docs candidate's content was written.  Restrict matching to
            # content-bearing fields and nested result envelopes.
            key_text = str(key or "").casefold()
            if key_text in {
                "outline_text",
                "title",
                "description",
                "fields_json",
                "add_tags",
                "remove_tags",
                "append_text",
                "content",
                "body",
                "body_text",
                "document_json",
                "changed",
                "docs",
                "result",
                "output",
            } or isinstance(child, (dict, list)):
                yield from _iter_text_values(child, _depth=_depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in list(value)[:64]:
            yield from _iter_text_values(child, _depth=_depth + 1)


def _candidate_content_values(content: dict[str, Any]) -> list[str]:
    content_value = _normal_text(content.get("content"))
    if content_value and len(content_value) >= 8:
        # A semantic candidate's body is the only safe proof that a direct
        # write fulfilled it.  A generic title (for example, "Project
        # information") must not suppress an unrelated body suggestion.
        return [content_value]
    title_value = _normal_text(content.get("title"))
    return [title_value] if title_value and len(title_value) >= 8 else []


def _receipt_satisfies_candidate(receipt: Any, content: dict[str, Any]) -> bool:
    candidate_values = _candidate_content_values(content)
    if not candidate_values:
        return False
    receipt_values = [
        _normal_text(value)
        for value in _iter_text_values(
            {
                "arguments": getattr(receipt, "arguments", None)
                if not isinstance(receipt, dict)
                else receipt.get("arguments"),
                "result": getattr(receipt, "result", None)
                if not isinstance(receipt, dict)
                else receipt.get("result"),
                "metadata": getattr(receipt, "result_metadata", None)
                if not isinstance(receipt, dict)
                else receipt.get("metadata"),
            }
        )
        if _normal_text(value)
    ]
    for candidate in candidate_values:
        for receipt_value in receipt_values:
            if not receipt_value:
                continue
            # The complete candidate body must appear in the durable receipt.
            # Do not accept a short receipt fragment as proof for a longer
            # suggestion; that would suppress unrelated inferred content.
            if candidate in receipt_value:
                return True
    return False


async def has_matching_successful_docs_mutation(
    *,
    project_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    source_message_id: uuid.UUID | str | None = None,
    agent_run_id: uuid.UUID | str | None = None,
    content: dict[str, Any],
) -> bool:
    """Check the durable AgentRun receipt for an explicit Docs mutation.

    This is intentionally receipt/ledger based: no natural-language command
    matching is performed.  The query is scoped by Project and actor before
    content comparison, so a successful write in another turn or Project
    cannot suppress an inferred candidate.
    """

    try:
        project_uuid = uuid.UUID(str(project_id))
    except (TypeError, ValueError, AttributeError):
        return False
    source_uuid = None
    run_uuid = None
    try:
        if source_message_id:
            source_uuid = uuid.UUID(str(source_message_id))
    except (TypeError, ValueError, AttributeError):
        source_uuid = None
    try:
        if agent_run_id:
            run_uuid = uuid.UUID(str(agent_run_id))
    except (TypeError, ValueError, AttributeError):
        run_uuid = None

    async with await get_db_session() as session:
        try:
            statement = (
                select(AgentRunToolCall, AgentRun)
                .join(AgentRun, AgentRun.id == AgentRunToolCall.run_id)
                .where(
                    AgentRun.project_id == project_uuid,
                    AgentRun.user_id == str(user_id),
                    AgentRunToolCall.tool_name.in_(_DOCS_MUTATION_TOOL_NAMES),
                    AgentRunToolCall.success.is_(True),
                    AgentRunToolCall.mutation_confirmed.is_(True),
                )
                .order_by(AgentRunToolCall.created_at.desc())
                .limit(32)
            )
            if run_uuid is not None:
                # Direct Docs mutations may execute in a delegated child
                # AgentRun.  Include the run's immediate/root descendants,
                # whose project and actor are inherited by AgentRunService;
                # this remains turn-scoped and cannot cross an ACL boundary.
                statement = statement.where(
                    or_(
                        AgentRun.id == run_uuid,
                        AgentRun.root_run_id == run_uuid,
                        AgentRun.parent_run_id == run_uuid,
                    )
                )
            elif source_uuid is not None:
                statement = statement.where(AgentRun.trigger_message_id == source_uuid)
            else:
                return False
            result = await session.execute(statement)
            if hasattr(result, "all"):
                rows = result.all()
            elif hasattr(result, "scalars"):
                scalar_result = result.scalars()
                rows = scalar_result.all() if hasattr(scalar_result, "all") else []
            else:
                rows = []
            for row in rows:
                try:
                    receipt = row[0]
                except (IndexError, KeyError, TypeError):
                    receipt = row
                if _receipt_satisfies_candidate(receipt, content):
                    return True
        except Exception:
            # A missing optional AgentRun ledger (for legacy voice/CLI turns)
            # must never fail the memory job or widen suppression.
            logger.debug("Docs mutation receipt lookup unavailable", exc_info=True)
        return False


async def persist_project_qa_candidates_for_job(
    *,
    project_id: uuid.UUID | str,
    user_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    source_message_id: uuid.UUID | str | None,
    source_job_id: uuid.UUID | str,
    candidates: list[dict[str, Any]],
    user_input: str,
    assistant_response: str,
    agent_run_id: uuid.UUID | str | None = None,
) -> dict[str, int]:
    """Persist semantic Q&A candidates emitted by a completed memory job."""

    try:
        project_uuid = uuid.UUID(str(project_id))
    except (TypeError, ValueError, AttributeError):
        return {"created": 0, "updated": 0, "skipped": len(candidates or [])}
    try:
        session_uuid = uuid.UUID(str(session_id))
    except (TypeError, ValueError, AttributeError):
        session_uuid = None
    source_job = str(source_job_id or "").strip()
    if not source_job:
        return {"created": 0, "updated": 0, "skipped": len(candidates or [])}
    source_agent_run = _coerce_uuid(agent_run_id)
    source_message = str(source_message_id or "").strip() or None
    source_message_uuid = _coerce_uuid(source_message) if source_message else None
    if session_uuid is None:
        return {"created": 0, "updated": 0, "skipped": len(candidates or [])}

    async with await get_db_session() as session:
        # Take the parent lifecycle lock before any ACL decision or dependent
        # source-row validation.  A stale identity-map Project must not allow
        # a candidate write after completion/deletion commits concurrently.
        project = await _load_project_for_update(session, project_uuid)
        if project is None or getattr(project, "deleted_at", None) is not None:
            return {"created": 0, "updated": 0, "skipped": len(candidates or [])}
        if bool(getattr(project, "is_completed", False)):
            return {"created": 0, "updated": 0, "skipped": len(candidates or [])}
        if source_message_uuid is None:
            # Automatic Project Q&A must be grounded in a canonical persisted
            # user message; a session id alone is insufficient provenance.
            return {"created": 0, "updated": 0, "skipped": len(candidates or [])}
        # Persist one canonical UUID spelling so equivalent casing/whitespace
        # cannot split exact source provenance or reconciliation identities.
        source_message = str(source_message_uuid)
        # Do not promote a masking source into a reusable Project Q&A entry.
        # The original user row is intentionally durable, so check its
        # server-issued metadata rather than relying on the caller to omit a
        # source_message_id.
        if source_message:
            if source_message_uuid is not None:
                try:
                    source_row = await session.get(
                        ConversationMessage,
                        source_message_uuid,
                    )
                except Exception:
                    source_row = None
                if source_row is not None and is_privacy_masking_source(source_row):
                    return {
                        "created": 0,
                        "updated": 0,
                        "skipped": len(candidates or []),
                    }
        # A durable job carries the original authenticated principal as a
        # string. Never turn an invalid/external principal into the project
        # owner: background curation is a write and must re-check the same
        # canonical Project ACL as an interactive mutation in this transaction.
        creator = _coerce_uuid(user_id)
        if creator is None:
            return {"created": 0, "updated": 0, "skipped": len(candidates or [])}
        actor = await session.get(User, creator)
        if actor is None:
            return {"created": 0, "updated": 0, "skipped": len(candidates or [])}
        member = await session.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project_uuid,
                ProjectMember.user_id == creator,
            )
        )
        if not has_effective_project_permission(
            user_id=creator,
            user_role=getattr(actor, "role", None),
            project_owner_id=getattr(project, "owner_id", None),
            member_permissions=(
                getattr(member, "permissions", None) if member else None
            ),
            permission="write",
        ):
            return {"created": 0, "updated": 0, "skipped": len(candidates or [])}
        # Re-lock the exact source conversation/message in this same
        # transaction as candidate mutation.  The process-level preflight is
        # intentionally not sufficient: a reassignment or deletion racing
        # between preflight and this write must make the QA curation a no-op.
        if not await _validate_source_chat_binding(
            session,
            project_id=project_uuid,
            actor_id=creator,
            session_id=session_uuid,
            source_message_id=source_message_uuid,
            user_input=user_input,
        ):
            return {"created": 0, "updated": 0, "skipped": len(candidates or [])}
        # ``ensure_project_information_doc`` takes the Project parent lock
        # before its Docs-library advisory/node locks.  We already hold that
        # same parent-first order here, avoiding a QA child/advisory -> Project
        # inversion while still rechecking lifecycle state immediately before
        # any candidate mutation.
        try:
            node = await ensure_project_information_doc(
                session,
                project=project,
                user_id=creator,
            )
        except ValueError:
            # ``ensure_project_information_doc`` performs its own locked
            # completion/deletion recheck.  Translate that race into a safe
            # no-op while preserving unrelated validation failures.
            current_project = await session.get(Project, project_uuid)
            if (
                current_project is None
                or getattr(current_project, "deleted_at", None) is not None
                or bool(getattr(current_project, "is_completed", False))
            ):
                return {
                    "created": 0,
                    "updated": 0,
                    "skipped": len(candidates or []),
                }
            raise
        project = await _load_project_for_update(session, project_uuid)
        if project is None or getattr(project, "deleted_at", None) is not None:
            return {"created": 0, "updated": 0, "skipped": len(candidates or [])}
        if bool(getattr(project, "is_completed", False)):
            return {"created": 0, "updated": 0, "skipped": len(candidates or [])}
        created = updated = skipped = 0
        seen_hashes: set[str] = set()
        for raw_candidate in candidates[:PROJECT_QA_MAX_CANDIDATES]:
            normalized = normalize_project_qa_candidate(
                raw_candidate,
                user_input=user_input,
                assistant_response=assistant_response,
            )
            if normalized is None:
                skipped += 1
                continue
            sensitivity, _ = classify_sensitivity_fields(
                normalized.get("question"),
                normalized.get("answer"),
                normalized.get("evidence_span"),
                normalized.get("answer_evidence_span"),
            )
            if sensitivity != "normal":
                # Q&A remains review-only, but review is not a bypass around
                # the same provider-output sensitivity boundary as Memory and
                # Docs candidates.  Do not persist secrets or sensitive PII
                # in a candidate that a later reviewer could expose.
                skipped += 1
                continue
            question = normalized["question"]
            question_hash = _normalized_question_hash(question)
            if question_hash in seen_hashes:
                skipped += 1
                continue
            seen_hashes.add(question_hash)

            entry = await find_existing_project_qa_entry(
                session,
                project_id=project_uuid,
                question=question,
                question_hash=question_hash,
            )
            if entry is not None:
                if is_project_qa_entry_closed(entry) or _entry_has_source_job(entry, source_job):
                    skipped += 1
                    continue
            if entry is None:
                refs = [
                    {
                        "type": "scoped_memory_job",
                        "id": source_job,
                        "session_id": str(session_uuid) if session_uuid else None,
                    }
                ]
                if source_agent_run is not None:
                    refs[0]["agent_run_id"] = str(source_agent_run)
                if normalized.get("answer_evidence_span"):
                    refs.append(
                        {
                            "type": "assistant_response",
                            "source_job_id": source_job,
                            # Keep only a verifiable digest; the bounded raw
                            # assistant span is evidence for validation, not a
                            # second transcript store.
                            "evidence_sha256": hashlib.sha256(
                                normalized["answer_evidence_span"].encode("utf-8")
                            ).hexdigest(),
                        }
                    )
                entry_kwargs: dict[str, Any] = {
                    "project_id": project_uuid,
                    "knowledge_node_id": node.id,
                    "question": question,
                    "answer": normalized.get("answer"),
                    "normalized_question_hash": question_hash,
                    "status": "answered" if normalized.get("answer") else "unanswered",
                    "review_state": "candidate",
                    "confidence": normalized.get("confidence", 0.0),
                    "asked_count": 1,
                    "source_session_id": session_uuid,
                    "source_message_ids": [source_message] if source_message else [],
                    "source_agent_run_ids": (
                        [str(source_agent_run)] if source_agent_run is not None else []
                    ),
                    "answer_source_refs": refs,
                    "created_by": creator,
                    "updated_by": creator,
                    "created_by_agent": True,
                }
                entry = ProjectQaEntry(**entry_kwargs)
                # Newer schema revisions may expose explicit provenance
                # columns.  Keep compatibility with older deployments.
                if hasattr(entry, "origin"):
                    # ``legacy_auto`` is reserved for rows produced by the
                    # pre-semantic raw-message path. New completed-turn
                    # semantic curation must survive legacy cleanup.
                    entry.origin = "semantic_turn"
                if hasattr(entry, "source_job_id"):
                    try:
                        entry.source_job_id = uuid.UUID(source_job)
                    except (TypeError, ValueError, AttributeError):
                        pass
                session.add(entry)
                created += 1
                continue

            # Merge a repeated semantic question into an existing candidate,
            # but never reopen accepted/closed rows.  A newly supported answer
            # may fill an unanswered candidate while review_state remains
            # ``candidate`` and therefore is still gated from context.
            now = datetime.utcnow()
            old_sources = list(getattr(entry, "source_message_ids", None) or [])
            if source_message and source_message not in old_sources:
                old_sources.append(source_message)
                entry.source_message_ids = old_sources[:32]
                entry.asked_count = int(entry.asked_count or 0) + 1
            elif not source_message:
                # Some legacy/CLI turns have no persisted message id.  The
                # source-job id has already established that this is a new
                # turn, so retain asked-count semantics without appending an
                # unusable placeholder to source_message_ids.
                entry.asked_count = int(entry.asked_count or 0) + 1
            if source_agent_run is not None:
                _append_string_ref(entry, "source_agent_run_ids", source_agent_run)
            refs = [
                {
                    "type": "scoped_memory_job",
                    "id": source_job,
                    "session_id": str(session_uuid) if session_uuid else None,
                }
            ]
            if source_agent_run is not None:
                refs[0]["agent_run_id"] = str(source_agent_run)
            if normalized.get("answer_evidence_span"):
                refs.append(
                    {
                        "type": "assistant_response",
                        "source_job_id": source_job,
                        "evidence_sha256": hashlib.sha256(
                            normalized["answer_evidence_span"].encode("utf-8")
                        ).hexdigest(),
                    }
                )
            if not getattr(entry, "answer", None) and normalized.get("answer"):
                entry.answer = normalized["answer"]
                entry.status = "answered"
            for ref in refs:
                _append_source_ref(entry, ref)
            entry.last_asked_at = now
            entry.updated_at = now
            entry.updated_by = creator
            if hasattr(entry, "origin") and not getattr(entry, "origin", None):
                entry.origin = "legacy_auto"
            if hasattr(entry, "version"):
                entry.version = int(getattr(entry, "version", 1) or 1) + 1
            updated += 1

        await session.commit()
        return {"created": created, "updated": updated, "skipped": skipped}


async def find_existing_project_qa_entry(
    session: Any,
    *,
    project_id: uuid.UUID,
    question: str,
    question_hash: str | None = None,
) -> ProjectQaEntry | None:
    """Find active or terminal Q&A without reopening legacy tombstones."""
    normalized_hash = question_hash or _normalized_question_hash(question)
    bind = session.get_bind() if hasattr(session, "get_bind") else None
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect_name == "postgresql":
        # Serialize all Q&A writers per project. Hash-level locks can deadlock when
        # two drafts contain the same questions in opposite order.
        lock_key = f"project-qa:{project_id}"
        await session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(lock_key)))
        )

    result = await session.execute(
        select(ProjectQaEntry)
        .where(
            ProjectQaEntry.project_id == project_id,
            ProjectQaEntry.normalized_question_hash == normalized_hash,
        )
        # During the migration window duplicate hashes can exist.  Prefer a
        # closed/accepted row so automatic curation cannot update a second,
        # still-open duplicate while leaving the durable answer untouched.
        .order_by(
            case(
                (
                    ProjectQaEntry.review_state.in_(("accepted", "rejected")),
                    0,
                ),
                (
                    ProjectQaEntry.status.in_(tuple(_CLOSED_QA_STATUSES)),
                    0,
                ),
                (ProjectQaEntry.origin == "manual", 0),
                else_=1,
            ),
            ProjectQaEntry.updated_at.desc(),
        )
        .limit(1)
    )
    entry = result.scalar_one_or_none()
    if entry is not None:
        return entry

    legacy_result = await session.execute(
        select(ProjectQaEntry).where(
            ProjectQaEntry.project_id == project_id,
            ProjectQaEntry.normalized_question_hash.is_(None),
        )
    )
    legacy_candidates = [
        candidate
        for candidate in legacy_result.scalars().all()
        if _normalized_question_hash(candidate.question) == normalized_hash
    ]
    entry = next(
        (candidate for candidate in legacy_candidates if is_project_qa_entry_closed(candidate)),
        legacy_candidates[0] if legacy_candidates else None,
    )
    if entry is not None:
        entry.normalized_question_hash = normalized_hash
    return entry


def extract_project_qa_candidate_questions(content: str, *, limit: int = 5) -> list[str]:
    """Extract reusable project questions from one chat message."""

    candidates: list[str] = []
    seen: set[str] = set()
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip(" \t-・*　")
        if not line:
            continue
        parts = re.split(r"(?<=[。！？?])\s*", line)
        for part in parts:
            question = part.strip(" \t　")
            if len(question) < 4 or len(question) > 240:
                continue
            if not _QUESTION_HINT_RE.search(question):
                continue
            if _is_noise_question_candidate(question):
                continue
            if not question.endswith(("?", "？")) and re.search(r"(ですか|ますか|でしょうか|必要ですか|ありますか|できますか)$", question):
                question += "？"
            key = " ".join(question.casefold().split())
            if key in seen:
                continue
            seen.add(key)
            candidates.append(question)
            if len(candidates) >= limit:
                return candidates
    return candidates


def queue_project_qa_candidate_extraction(message_id: Any) -> bool:
    """Schedule best-effort Q&A candidate extraction for a persisted user message."""

    parsed = _coerce_uuid(message_id)
    if parsed is None:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    loop.create_task(process_project_qa_candidates_for_message(parsed))
    return True


async def process_project_qa_candidates_for_message(message_id: uuid.UUID) -> dict[str, Any]:
    """Compatibility no-op for the retired user-only ingestion path.

    Project Q&A is now produced only from a completed user+assistant
    ``ScopedMemoryJob``.  The function remains importable for old integrations
    and migration scripts, but it deliberately never calls the legacy regex
    extractor or writes a row from a lone user message.
    """

    now = datetime.utcnow()
    async with await get_db_session() as session:
        try:
            result = await session.execute(
                select(ConversationMessage, ConversationSession, Project)
                .join(ConversationSession, ConversationMessage.session_id == ConversationSession.id)
                .join(Project, ConversationSession.project_id == Project.id)
                .where(ConversationMessage.id == message_id)
                .with_for_update(of=ConversationMessage)
            )
            row = result.one_or_none()
            if row is None:
                return {"success": False, "reason": "message_or_project_not_found"}

            message, conversation, project = row
            if message.role != "user" or not conversation.project_id:
                return {"success": True, "skipped": "not_project_user_message"}

            metadata = dict(message.message_metadata or {})
            if is_privacy_masking_source(metadata):
                return {
                    "success": True,
                    "skipped": "privacy_masking_source",
                    "created": 0,
                    "updated": 0,
                }
            existing_job = metadata.get("project_qa_candidate_job")
            if isinstance(existing_job, dict) and existing_job.get("status") == "done":
                return {"success": True, "skipped": "already_done"}
            metadata["project_qa_candidate_job"] = {
                "status": "running",
                "started_at": now.isoformat(),
                "question_count": 0,
                "reason": "completed_turn_required",
            }
            message.message_metadata = metadata
            await session.flush()

            completed_metadata = dict(metadata)
            completed_metadata["project_qa_candidate_job"] = {
                "status": "done",
                "started_at": now.isoformat(),
                "finished_at": datetime.utcnow().isoformat(),
                "question_count": 0,
                "created": 0,
                "updated": 0,
                "reason": "completed_turn_required",
            }
            message.message_metadata = completed_metadata
            await session.commit()
            return {"success": True, "created": 0, "updated": 0}
        except Exception:
            await session.rollback()
            logger.exception(
                "[ProjectQACandidate] failed to process message %s",
                message_id,
            )
            return {"success": False, "error": "project_qa_candidate_failed"}


__all__ = [
    "PROJECT_QA_ARTIFACT_TYPES",
    "PROJECT_QA_MAX_CANDIDATES",
    "PROJECT_QA_MIN_CONFIDENCE",
    "extract_project_qa_candidate_questions",
    "find_existing_project_qa_entry",
    "has_matching_successful_docs_mutation",
    "is_project_qa_artifact",
    "is_project_qa_entry_closed",
    "normalize_project_qa_candidate",
    "persist_project_qa_candidates_for_job",
    "process_project_qa_candidates_for_message",
    "queue_project_qa_candidate_extraction",
    "_normalized_question_hash",
]
