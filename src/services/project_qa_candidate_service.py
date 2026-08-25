"""Create project Q&A candidates from saved chat turns."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from ..memory.database import get_db_session
from ..memory.models import ConversationMessage, ConversationSession, Project, ProjectQaEntry
from .project_information_docs import ensure_project_information_doc

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


_CLOSED_QA_STATUSES = frozenset({"answered", "resolved", "stale", "cancelled", "archived"})


def is_project_qa_entry_closed(entry: ProjectQaEntry) -> bool:
    """Return whether an automatic intake must leave this canonical row untouched."""
    return (
        entry.deleted_at is not None
        or str(entry.status or "").strip().lower() in _CLOSED_QA_STATUSES
        or str(entry.review_state or "").strip().lower() == "rejected"
    )


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
    entry = next(
        (
            candidate
            for candidate in legacy_result.scalars().all()
            if _normalized_question_hash(candidate.question) == normalized_hash
        ),
        None,
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
    """Process one persisted user message and upsert candidate Project Q&A rows."""

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
            existing_job = metadata.get("project_qa_candidate_job")
            if isinstance(existing_job, dict) and existing_job.get("status") == "done":
                return {"success": True, "skipped": "already_done"}

            questions = extract_project_qa_candidate_questions(message.content)
            metadata["project_qa_candidate_job"] = {
                "status": "running",
                "started_at": now.isoformat(),
                "question_count": len(questions),
            }
            message.message_metadata = metadata
            await session.flush()

            if not questions:
                completed_metadata = dict(metadata)
                completed_metadata["project_qa_candidate_job"] = {
                    "status": "done",
                    "started_at": now.isoformat(),
                    "finished_at": datetime.utcnow().isoformat(),
                    "question_count": 0,
                }
                message.message_metadata = completed_metadata
                await session.commit()
                return {"success": True, "created": 0, "updated": 0}

            user_id = _coerce_uuid(conversation.user_id) or project.owner_id
            node = await ensure_project_information_doc(
                session,
                project=project,
                user_id=user_id,
            )

            created = 0
            updated = 0
            for question in questions:
                question_hash = _normalized_question_hash(question)
                entry = await find_existing_project_qa_entry(
                    session,
                    project_id=project.id,
                    question=question,
                    question_hash=question_hash,
                )
                if entry is None:
                    entry = ProjectQaEntry(
                        project_id=project.id,
                        knowledge_node_id=node.id,
                        question=question,
                        answer=None,
                        normalized_question_hash=question_hash,
                        status="unanswered",
                        review_state="candidate",
                        confidence=0.65,
                        asked_count=1,
                        source_session_id=conversation.id,
                        source_message_ids=[str(message.id)],
                        created_by=user_id,
                        updated_by=user_id,
                        created_by_agent=True,
                    )
                    session.add(entry)
                    created += 1
                elif is_project_qa_entry_closed(entry):
                    continue
                else:
                    entry.asked_count = int(entry.asked_count or 0) + 1
                    entry.last_asked_at = now
                    entry.updated_at = now
                    entry.updated_by = user_id
                    sources = list(entry.source_message_ids or [])
                    if str(message.id) not in sources:
                        sources.append(str(message.id))
                    entry.source_message_ids = sources
                    updated += 1

            completed_metadata = dict(metadata)
            completed_metadata["project_qa_candidate_job"] = {
                "status": "done",
                "started_at": now.isoformat(),
                "finished_at": datetime.utcnow().isoformat(),
                "question_count": len(questions),
                "created": created,
                "updated": updated,
            }
            message.message_metadata = completed_metadata
            await session.commit()
            return {"success": True, "created": created, "updated": updated}
        except Exception:
            await session.rollback()
            logger.exception(
                "[ProjectQACandidate] failed to process message %s",
                message_id,
            )
            return {"success": False, "error": "project_qa_candidate_failed"}
