"""Derived EG2 index for canonical ContextMemory; never a second memory store."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import replace
from datetime import datetime

from ..rag.embedding_identity import DIMENSION, EMBEDDING_FINGERPRINT, document_input, embedding_collection_name
from .models import ContextMemory, ConversationSession, Project, Task

INPUT_VERSION = 1
COLLECTION_NAME = embedding_collection_name("aoitalk_context_memory")
ACL_BATCH_SIZE = 500


class ContextMemoryIndexUnavailable(RuntimeError):
    def __init__(self, reason="context_memory_index_unavailable"):
        self.reason = reason
        super().__init__(reason)


def input_hash(row):
    value = [INPUT_VERSION, document_input(str(row.content or ""), row.title)]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def binding(row):
    """Metadata-only binding; original prose/provenance stays in Postgres."""
    return {"source_kind": "context_memory", "memory_id": str(row.id),
            "memory_version": int(row.version), "owner_key": row.user_id,
            "scope_type": row.scope_type, "scope_id": row.scope_id,
            "project_id": str(row.project_id) if row.project_id else None,
            "task_id": str(row.task_id) if row.task_id else None,
            "session_id": str(row.session_id) if row.session_id else None,
            "original_updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "input_version": INPUT_VERSION}


def matches(payload, row, *, verify_input=True):
    return (isinstance(payload, dict)
            and all(payload.get(key) == value for key, value in binding(row).items())
            and payload.get("embedding_fingerprint") == EMBEDDING_FINGERPRINT
            and (not verify_input or payload.get("embedding_input_hash") == input_hash(row)))


async def globally_live(session, row):
    """Withdrawal needs only lifecycle metadata and grants no original access."""
    if (row is None or row.status != "active"
            or row.expires_at is not None and row.expires_at <= datetime.utcnow()):
        return False
    # Provenance IDs on user/global rows do not change their canonical scope.
    if row.scope_type == "task":
        task = await session.get(Task, row.task_id, populate_existing=True)
        if task is None or task.deleted_at is not None:
            return False
        project = await session.get(Project, task.project_id, populate_existing=True)
        return project is not None and project.deleted_at is None
    model, identifier = (Project, row.project_id) if row.scope_type == "project" else (
        ConversationSession, row.session_id) if row.scope_type == "session" else (None, None)
    if model is not None:
        parent = await session.get(model, identifier, populate_existing=True)
        return parent is not None and parent.deleted_at is None
    return True


async def indexing_actor(session, row):
    """Use the current canonical Project owner for shared scope recovery."""
    project_id = row.project_id if row.scope_type == "project" else None
    if row.scope_type == "task":
        task = await session.get(Task, row.task_id, populate_existing=True)
        project_id = task.project_id if task is not None else None
    if project_id is not None:
        project = await session.get(Project, project_id, populate_existing=True)
        if project is not None and project.owner_id is not None:
            return str(project.owner_id)
    return row.user_id


class ContextMemoryIndex:
    def __init__(self, session_factory, *, encoder=None, qdrant=None):
        self.session_factory = session_factory
        self.encoder, self.qdrant = encoder, qdrant

    async def initialize(self):
        if self.encoder is None or self.qdrant is None:
            from ..rag.config import get_rag_config
            from ..rag.embedding import get_embedding
            from ..rag.qdrant_client import QdrantManager
            config = get_rag_config()
            self.encoder = self.encoder or get_embedding(config.embedding)
            self.qdrant = self.qdrant or QdrantManager(
                replace(config.qdrant, collection_name=COLLECTION_NAME), vector_size=DIMENSION)
        if not await self.qdrant.initialize():
            raise ContextMemoryIndexUnavailable()

    async def _points(self, identifiers, *, vectors=False):
        return await self.qdrant._io(
            self.qdrant.client.retrieve, collection_name=COLLECTION_NAME,
            ids=[str(uuid.uuid5(uuid.NAMESPACE_URL, "aoitalk:" + str(value))) for value in identifiers],
            with_payload=True, with_vectors=vectors)

    async def _delete(self, identifier):
        from qdrant_client import models
        await self.qdrant._io(self.qdrant.client.delete, collection_name=COLLECTION_NAME,
            points_selector=models.PointIdsList(points=[str(uuid.uuid5(uuid.NAMESPACE_URL, "aoitalk:" + str(identifier)))]))

    async def _current(self, actor, identifier):
        from ..services.scoped_memory_service import ScopedMemoryService
        async with await self.session_factory() as session:
            row = await session.get(ContextMemory, uuid.UUID(str(identifier)), populate_existing=True)
            if not await globally_live(session, row):
                return None
            service = ScopedMemoryService(self.session_factory)
            await service._require_scope_permission(session, scope=service._scope_from_memory(row),
                                                    actor_id=actor, write=False)
            # Access the encrypted content only after canonical permission.
            input_hash(row)
            return row

    async def _successor_id(self, identifier):
        from sqlalchemy import select
        cursor, seen = uuid.UUID(str(identifier)), set()
        async with await self.session_factory() as session:
            while cursor not in seen:
                seen.add(cursor)
                children = list(await session.execute(select(ContextMemory.id, ContextMemory.status).where(
                    ContextMemory.supersedes_id == cursor,
                    ContextMemory.status.in_(["active", "superseded"])).limit(2)))
                if len(children) != 1:
                    return None
                cursor, status = children[0]
                if status == "active":
                    return cursor
        return None

    async def _reuse_vector(self, row, digest):
        from sqlalchemy import select
        cursor, seen = row.id, set()
        async with await self.session_factory() as session:
            while cursor is not None and cursor not in seen:
                seen.add(cursor)
                for point in await self._points([cursor], vectors=True):
                    payload = point.payload or {}
                    if (payload.get("owner_key") == row.user_id and payload.get("source_kind") == "context_memory"
                            and payload.get("input_version") == INPUT_VERSION
                            and payload.get("embedding_input_hash") == digest
                            and await self.qdrant.has_verified_document(payload.get("doc_id", ""), digest)):
                        return point.vector
                cursor = (await session.execute(select(ContextMemory.supersedes_id).where(
                    ContextMemory.id == cursor, ContextMemory.user_id == row.user_id))).scalar_one_or_none()
        return None

    async def sync_source(self, actor, identifier):
        from ..services.scoped_memory_service import ScopedMemoryError
        await self.initialize()
        try:
            row = await self._current(actor, identifier)
        except ScopedMemoryError:
            # A revoked indexing actor must not delete another legal reader's point.
            return {"status": "excluded", "reason": "context_memory_actor_not_authorized"}
        if row is None:
            # Queue order is by source ID, not replacement order. Preserve an
            # unchanged verified vector in its active successor before removing
            # this historical point; original ACL is checked by sync_source.
            successor = await self._successor_id(identifier)
            points = await self._points([identifier]) if successor is not None else []
            if points:
                try:
                    current = await self._current(actor, successor)
                    payload = points[0].payload or {}
                    if (current is not None and payload.get("owner_key") == current.user_id
                            and payload.get("embedding_input_hash") == input_hash(current)):
                        await self.sync_source(actor, successor)
                except (ContextMemoryIndexUnavailable, ScopedMemoryError):
                    # The successor's durable job retries independently.
                    pass
            await self._delete(identifier)
            return {"status": "excluded", "reason": "context_memory_not_live"}
        digest = input_hash(row)
        points = await self._points([row.id], vectors=True)
        saved = points[0] if len(points) == 1 else None
        if saved and matches(saved.payload, row) and await self.qdrant.has_verified_document(str(row.id), digest):
            return {"status": "ready"}
        # Reuse only an exact verified input in this row's canonical lineage.
        vector = await self._reuse_vector(row, digest)
        if vector is None:
            if not await self.encoder.initialize():
                raise ContextMemoryIndexUnavailable("context_memory_encoder_unavailable")
            vectors = await self.encoder.embed([str(row.content or "")], titles=[row.title])
            if len(vectors) != 1:
                raise ContextMemoryIndexUnavailable("context_memory_invalid_vector")
            vector = vectors[0]
        current = await self._current(actor, identifier)
        if current is None or binding(current) != binding(row) or input_hash(current) != digest:
            raise ContextMemoryIndexUnavailable("context_memory_changed_during_indexing")
        metadata = {**binding(row), "embedding_input_hash": digest}
        # No duplicate plaintext body/title in the derivative payload.
        if not await self.qdrant.add_documents([str(row.id)], [vector], [""], [metadata]):
            raise ContextMemoryIndexUnavailable("context_memory_write_failed")
        if not await self.qdrant.has_verified_document(str(row.id), digest):
            await self._delete(identifier)
            raise ContextMemoryIndexUnavailable("context_memory_persisted_vector_invalid")
        current = await self._current(actor, identifier)
        if current is None or not matches(metadata | {"embedding_fingerprint": EMBEDDING_FINGERPRINT}, current):
            await self._delete(identifier)
            raise ContextMemoryIndexUnavailable("context_memory_changed_during_indexing")
        return {"status": "ready"}

    async def search(self, service, scope_ids, query, *, limit=24):
        """Search every currently authorized ID, with bounded ID/vector pages."""
        await self.initialize()
        if not await self.encoder.initialize():
            raise ContextMemoryIndexUnavailable("context_memory_encoder_unavailable")
        vector = await self.encoder.embed_query(query)
        if (len(vector) != DIMENSION or any(not math.isfinite(float(value)) for value in vector)
                or abs(sum(float(value) ** 2 for value in vector) - 1) > .01):
            raise ContextMemoryIndexUnavailable("context_memory_invalid_query_vector")
        best = {}
        async with await service._new_session() as session:
            statement = await service._active_retrieval_statement(session, scope_ids)
            rows = await session.stream_scalars(statement.order_by(ContextMemory.id).execution_options(yield_per=ACL_BATCH_SIZE))
            batch = []
            try:
                async for row in rows:
                    batch.append(row)
                    if len(batch) == ACL_BATCH_SIZE:
                        await self._search_batch(batch, vector, best, limit)
                        batch.clear()
                if batch:
                    await self._search_batch(batch, vector, best, limit)
            finally:
                await rows.close()
        # An updated/forgotten/revoked source never survives hydration.
        async with await service._new_session() as session:
            statement = await service._active_retrieval_statement(session, scope_ids)
            current = (await session.execute(statement.where(ContextMemory.id.in_(
                [uuid.UUID(value) for value in best])))).scalars()
            result = []
            for row in current:
                hit = best[str(row.id)]
                if matches(hit.metadata, row):
                    result.append((float(hit.score), row, "semantic_match"))
            return sorted(result, key=lambda item: (item[0], str(item[1].id)), reverse=True)[:limit]

    async def _search_batch(self, rows, vector, best, limit):
        originals = {str(row.id): row for row in rows}
        # Require complete current coverage; never silently search a recency slice.
        points = await self._points(originals)
        covered = {point.payload.get("doc_id") for point in points
                   if point.payload and point.payload.get("doc_id") in originals
                   and matches(point.payload, originals[point.payload["doc_id"]], verify_input=False)}
        if covered != set(originals):
            raise ContextMemoryIndexUnavailable("context_memory_index_incomplete")
        hits = await self.qdrant.search(vector, top_k=min(400, limit * 4),
            filter_conditions={"doc_id": list(originals), "source_kind": "context_memory", "input_version": INPUT_VERSION},
            raise_on_error=True)
        for hit in hits:
            if hit.id in originals and matches(hit.metadata, originals[hit.id]):
                best[hit.id] = hit
        for key in sorted(best, key=lambda value: (best[value].score, value), reverse=True)[limit:]:
            del best[key]


_index = None


def get_context_memory_index():
    global _index
    if _index is None:
        from .database import get_db_session
        _index = ContextMemoryIndex(get_db_session)
    return _index
