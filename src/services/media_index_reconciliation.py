"""Bounded canonical metadata reconciliation for lost after-commit notifications.

Only registered source rows are scanned. No directory discovery, plaintext
body, token, original byte or vector is read at this recovery boundary.
The existing SQLite queue persists metadata hashes and keyset cursors.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from sqlalchemy import select, func, case, and_

from ..knowledge.service import KNOWLEDGE_SOURCE_READ_PERMISSIONS
from ..memory.models import (
    ConversationMessage, ConversationSession, ConversationParticipant, DocsLibrary, GeneratedMedia, User,
    GenerationOutput, KnowledgeAttachment, KnowledgeDocument, KnowledgeNode,
    KnowledgeSource, KnowledgeSourcePermission, MediaAssetRevision, MeetingProcessingJob, Project, Task,
    TaskAttachment, ContextMemory, WorkspaceFileEntry,
)

KINDS = ("generation_output", "generated_media", "chat_attachment", "task_attachment",
         "docs_attachment", "knowledge_document", "meeting_audio", "conversation_message", "context_memory",
         "workspace_file")
BATCH_SIZE = 100


def _actor(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _version(values):
    return hashlib.sha256(json.dumps(list(values), default=str, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


async def _conversation_readers(session, rows):
    """Choose an active current legal reader with bounded per-actor bulk ACL.

    One owner/participant snapshot feeds at most 100 source keys per actor.
    No persistent ACL cache is created; actor, Project and Space are freshly
    checked by the canonical resolver. Legal-reader changes enter the version.
    """
    from .search_sources import SearchScope, SearchSourceResolver, SourceRef
    from .privacy_masking_projection import is_privacy_masking_source

    owners = {_actor(row[1]) for row in rows} - {None}
    session_ids = {row[8] for row in rows}
    participants = await session.execute(select(ConversationParticipant.session_id, ConversationParticipant.participant_id).where(
            ConversationParticipant.session_id.in_(session_ids), ConversationParticipant.participant_type == "user",
            ConversationParticipant.status == "joined"))
    joined = {}
    for session_id, actor in participants:
        normalized = _actor(actor)
        if normalized is not None:
            joined.setdefault(session_id, set()).add(normalized)
    actor_ids = sorted(owners | {value for values in joined.values() for value in values})
    active = set()
    for start in range(0, len(actor_ids), 1000):
        active.update(str(value) for value in (await session.execute(select(User.id).where(
            User.id.in_([uuid.UUID(value) for value in actor_ids[start:start + 1000]]), User.is_active.is_(True)))).scalars())
    candidates, legal = {}, {}
    for row in rows:
        preferred = _actor(row[1])
        candidates[str(row[0])] = sorted((joined.get(row[8], set()) | {preferred}) & active)
    for actor in sorted({value for values in candidates.values() for value in values}):
        refs = [SourceRef("conversation_message", str(row[0])) for row in rows if actor in candidates[str(row[0])]]
        resolver = SearchSourceResolver(session, SearchScope(uuid.UUID(actor)))
        # Query errors retry the bounded page; incomplete permission evidence
        # must never become a new indexing actor or an ACK.
        allowed = set(await resolver.allowed_refs(refs, require_local_original=False))
        for ref in refs:
            if ref.key in allowed:
                legal.setdefault(ref.id, set()).add(actor)
    result = {}
    for row in rows:
        key, preferred = str(row[0]), _actor(row[1])
        readers = sorted(legal.get(key, set()))
        # Private-source cleanup reads only structural metadata and grants no
        # original access. A stale owner job can still withdraw its derivative.
        private = is_privacy_masking_source(row[5])
        options = candidates[key] if private else readers
        chosen = preferred if preferred in options else (options[0] if options else None)
        if private and chosen is None:
            # An inactive/missing author locates a metadata-only withdrawal
            # job. Its worker rejects original access before checking actors.
            chosen = preferred
        result[key] = chosen, readers
    return result


async def canonical_media_page(session, checkpoint, *, batch_size=BATCH_SIZE):
    kind, cursor, _ = checkpoint
    if not 1 <= batch_size <= BATCH_SIZE:
        raise ValueError("Media reconciliation page must be bounded to 100 rows")
    if kind == "generation_output":
        m = GenerationOutput
        revision = select(MediaAssetRevision.revision_hash).where(
            MediaAssetRevision.generation_output_id == m.id).order_by(
                MediaAssetRevision.created_at.desc(), MediaAssetRevision.id.desc()).limit(1).scalar_subquery()
        stmt = select(m.id, m.owner_user_id, m.sha256, m.byte_binding_state, m.project_id, revision)
    elif kind == "generated_media":
        m = GeneratedMedia
        stmt = select(m.id, m.owner_user_id, m.updated_at, m.status, m.relative_path,
                      m.context_type, m.context_id, m.bind_type, m.bind_id, m.byte_size)
    elif kind in {"chat_attachment", "conversation_message"}:
        m = ConversationMessage
        stmt = select(m.id, ConversationSession.user_id, m.updated_at, m.deleted_at,
                      m.is_active_branch, m.message_metadata, ConversationSession.project_id,
                      ConversationSession.deleted_at, ConversationSession.id,
                      ConversationSession.character_name).join(ConversationSession, m.session_id == ConversationSession.id)
        if kind == "chat_attachment":
            stmt = stmt.where(m.message_metadata['attachments'].as_string().is_not(None))
    elif kind == "task_attachment":
        m = TaskAttachment
        stmt = select(m.id, func.coalesce(Project.owner_id, m.created_by), m.file_path,
                      m.attachment_metadata, m.created_at, Task.deleted_at, m.project_id).join(
                          Task, m.task_id == Task.id).join(Project, m.project_id == Project.id)
    elif kind == "docs_attachment":
        m = KnowledgeAttachment
        stmt = select(m.id, func.coalesce(Project.owner_id, DocsLibrary.owner_user_id, m.created_by),
                      m.file_path, m.attachment_metadata, m.created_at, KnowledgeNode.updated_at,
                      KnowledgeNode.project_id, KnowledgeNode.docs_library_id).join(
                          KnowledgeNode, m.node_id == KnowledgeNode.id).join(
                              DocsLibrary, KnowledgeNode.docs_library_id == DocsLibrary.id).outerjoin(
                                  Project, KnowledgeNode.project_id == Project.id)
    elif kind == "knowledge_document":
        m = KnowledgeDocument
        granted_actor = select(func.coalesce(KnowledgeSourcePermission.user_id, Project.owner_id)).outerjoin(
            Project, KnowledgeSourcePermission.project_id == Project.id).where(
                KnowledgeSourcePermission.source_id == KnowledgeSource.id,
                func.lower(func.trim(KnowledgeSourcePermission.permission)).in_(KNOWLEDGE_SOURCE_READ_PERMISSIONS)).order_by(
                    KnowledgeSourcePermission.id).limit(1).correlate(KnowledgeSource).scalar_subquery()
        stmt = select(m.id, func.coalesce(KnowledgeSource.owner_user_id, granted_actor), m.updated_at, m.content_hash,
                      m.status, m.path, KnowledgeSource.updated_at, KnowledgeSource.source_type).join(
                          KnowledgeSource, m.source_id == KnowledgeSource.id)
    elif kind == "context_memory":
        m = ContextMemory
        # Exact opaque owner keys remain valid. Project/task workers use the
        # current Project owner; original access still requires canonical ACL.
        canonical_project = case((m.scope_type == "project", m.project_id),
                                 (m.scope_type == "task", Task.project_id), else_=None)
        stmt = select(m.id, func.coalesce(Project.owner_id.cast(m.user_id.type), m.user_id),
                      m.version, m.updated_at, m.status, m.expires_at,
                      m.user_id, m.scope_type, m.scope_id, m.project_id, m.task_id, m.session_id,
                      Project.deleted_at, Task.deleted_at, ConversationSession.deleted_at).outerjoin(
                          Task, m.task_id == Task.id).outerjoin(
                          Project, canonical_project == Project.id).outerjoin(
                          ConversationSession, m.session_id == ConversationSession.id)
    elif kind == "meeting_audio":
        m = MeetingProcessingJob
        stmt = select(m.id, m.actor_user_id, m.audio_sha256, m.status, m.updated_at, m.retry_generation)
    elif kind == "workspace_file":
        m = WorkspaceFileEntry
        # Version = identity-relevant metadata only. size/mtime are excluded so
        # a touch without a content change never re-queues; rel_path is
        # included so a move refreshes the payload without re-embedding.
        actor = case((m.scope == "user", m.owner_id), else_=Project.owner_id)
        stmt = select(m.id, actor, m.scope, m.owner_id, m.rel_path, m.content_hash, m.status,
                      m.excluded_reason, m.deleted_at, Project.deleted_at, m.name, m.size_bytes,
                      Project.id).outerjoin(
                          Project, and_(m.scope == "project", Project.id == m.owner_id))
    else:
        raise ValueError("Unsupported registered media source kind")
    stmt = stmt.order_by(m.id).limit(batch_size)
    if cursor:
        stmt = stmt.where(m.id > uuid.UUID(cursor))
    rows = list(await session.execute(stmt))
    readers = await _conversation_readers(session, rows) if kind in {"chat_attachment", "conversation_message"} and rows else {}
    now = datetime.utcnow()
    items = []
    for row in rows:
        if readers:
            actor, legal_readers = readers[str(row[0])]
            version = _version([*row[1:], legal_readers, actor])
        elif kind == "context_memory":
            from .scoped_memory_service import _canonical_actor_id
            actor = _canonical_actor_id(row[1]) if row[1] else None
            # The timestamp is unchanged when time passes. Include the
            # lifecycle transition so the usual queue withdraws the derived
            # point once, without re-encoding unchanged active originals.
            expired = row[5] is not None and row[5] <= now
            version = _version([*row[1:], expired])
        elif kind == "workspace_file":
            from .workspace_file_index import job_priority
            from .media_index_worker import PRIORITY_CLEANUP
            actor, version = _actor(row[1]), _version(row[1:10])
            cleanup = (row[6] == "deleted" or row[8] is not None
                       or row[2] == "project" and (row[9] is not None or row[12] is None))
            if row[6] == "pending_hash" and not cleanup:
                # Nothing to index until the scanner verified the bytes.
                actor = None
            priority = PRIORITY_CLEANUP if cleanup else job_priority(row[10], row[11] or 0)
            items.append((str(row[0]), actor, version, priority))
            continue
        else:
            actor, version = _actor(row[1]), _version(row[1:])
        if kind == "chat_attachment":
            attachments = (row[5] or {}).get("attachments") or []
            # Fail visibly rather than silently truncate a malformed inventory.
            if not isinstance(attachments, list) or len(attachments) > 1000:
                raise ValueError("Registered attachment inventory exceeds per-message bound")
            items.extend((f"{row[0]}:{index}", actor, version) for index in range(len(attachments)))
        else:
            items.append((str(row[0]), actor, version))
    return items, str(rows[-1][0]) if rows else cursor, len(rows) < batch_size
