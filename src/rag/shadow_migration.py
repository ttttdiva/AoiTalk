"""Read-only canonical DB adapters for a separately stored index rehearsal.

Only the supplied Qdrant destination and migration SQLite are writable. Live
ORM objects, vector_id, Docs delivery/revision rows, and original files never
become rehearsal checkpoints. The normal index input/point builders are reused
so a rehearsal evaluates the runtime's actual vector and lexical contracts.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from sqlalchemy import event, select, text

from ..knowledge.index_service import KnowledgeIndexService
from ..memory.models import (
    DocsLibrary, KnowledgeChunk, KnowledgeDocument, KnowledgeNode, KnowledgeNodeSupertag,
    KnowledgeSource, KnowledgeSupertag,
)
from ..security.field_crypto import decrypt_json_value_if_needed, decrypt_text_if_needed
from ..services.docs_graph_service import DocsGraphService
from ..services.search_sources import SourceRef, SourceUnavailable
from .docs_index import DocsIndexService, _content_hash, _node_text, _node_units
from .embedding_identity import EMBEDDING_FINGERPRINT
from .index_io import run_index_io
from .qdrant_client import SharedQdrantClient

# The runtime candidate-source cap is separate from source chunk/node count.
# A single library may contain the 145922-record evaluation corpus. Queries
# and encoder batches remain bounded; extraordinarily large manifests fail
# explicitly rather than silently truncating the rehearsal.
MAX_SHADOW_RECORDS = 1000000


@asynccontextmanager
async def read_only_source_session(session_factory):
    """No migrations/seed writes; reject flushes and enforce DB read-only DML.

    The caller supplies an already configured factory, never get_session()
    (which may initialize/migrate the application DB). One transaction lasts
    until rollback; PostgreSQL READ COMMITTED still rechecks current ACL rows.
    """
    session = session_factory()
    sync = session.sync_session

    def reject_flush(*_):
        raise RuntimeError("Shadow migration cannot flush the source database")

    def reject_commit(*_):
        raise RuntimeError("Shadow migration cannot commit the source database")

    event.listen(sync, "before_flush", reject_flush)
    event.listen(sync, "before_commit", reject_commit)
    sqlite_query_only = None
    try:
        dialect = sync.get_bind().dialect.name
        if dialect == "postgresql":
            await session.execute(text("SET TRANSACTION READ ONLY"))
        elif dialect == "sqlite":
            sqlite_query_only = await session.scalar(text("PRAGMA query_only"))
            await session.execute(text("PRAGMA query_only=ON"))
        else:
            raise RuntimeError("Unsupported read-only source database")
        yield session
    finally:
        try:
            await session.rollback()
        finally:
            try:
                if sqlite_query_only is not None:
                    await session.execute(text(f"PRAGMA query_only={int(sqlite_query_only)}"))
            finally:
                event.remove(sync, "before_flush", reject_flush)
                event.remove(sync, "before_commit", reject_commit)
                await session.close()


class ShadowCheckpoint:
    """Bind the entire destination store and its separate progress metadata.

    An actor's Docs eligibility can be only a subset of a shared library.
    Its canonical stale cleanup must never run in a store containing another
    actor/generation's partial index. Every shadow domain therefore shares
    this one destination binding, before opening Qdrant or an encoder.
    """

    target_marker_name = ".aoitalk-shadow-binding.sqlite"

    def __init__(self, ledger, *, generation, target_path, actor, source_binding):
        self.ledger = ledger
        self.target_path = Path(target_path).resolve()
        self.actor = str(actor)
        self.values = (generation, str(self.target_path), self.actor, source_binding, EMBEDDING_FINGERPRINT)
        # Reject mismatched/non-shadow progress before claiming a destination.
        with ledger.connect() as db:
            self._check_ledger_binding(db)
        self._bind_target_store()
        with ledger.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_ledger_binding(db)
            db.execute("""CREATE TABLE IF NOT EXISTS shadow_generation (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation TEXT NOT NULL,
                target_path TEXT NOT NULL, actor TEXT NOT NULL, source_binding TEXT NOT NULL,
                identity TEXT NOT NULL)""")
            db.execute("INSERT OR IGNORE INTO shadow_generation VALUES(1,?,?,?,?,?)", self.values)
            db.execute("""CREATE TABLE IF NOT EXISTS shadow_units (
                collection_name TEXT NOT NULL, source_key TEXT NOT NULL, unit_id TEXT NOT NULL,
                content_hash TEXT NOT NULL, embedding_fingerprint TEXT NOT NULL,
                PRIMARY KEY(collection_name,source_key,unit_id))""")

    def _check_ledger_binding(self, db):
        bound = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shadow_generation'").fetchone()
        if bound is None and db.execute("SELECT 1 FROM embedding_sources LIMIT 1").fetchone():
            raise RuntimeError("Existing non-shadow checkpoints cannot become a shadow generation")
        previous = db.execute("SELECT generation,target_path,actor,source_binding,identity FROM shadow_generation").fetchone() if bound else None
        if previous is not None and previous != self.values:
            raise RuntimeError("Shadow generation binding changed; choose another generation")

    def _bind_target_store(self):
        self.target_path.mkdir(parents=True, exist_ok=True)
        marker = self.target_path / self.target_marker_name
        if not marker.exists() and any(self.target_path.iterdir()):
            # Never auto-adopt an existing runtime, older rehearsal, or other
            # project's unbound Qdrant files. Choosing a new destination is
            # the recovery; this command does not clear/delete the old store.
            raise RuntimeError("Shadow target store is unbound and nonempty; choose a new empty destination")
        created = False
        try:
            # Only the process that exclusively creates a fresh marker may
            # establish its first binding. An interrupted/empty marker is
            # not proof of ownership of an already populated destination.
            with marker.open("xb"):
                created = True
        except FileExistsError:
            pass
        if marker.is_symlink() or not marker.is_file():
            raise RuntimeError("Shadow target store binding is invalid")
        with sqlite3.connect(marker, timeout=30) as db:
            # Atomic across independent manifests/processes: only one exact
            # actor/generation/source/identity may claim this destination.
            db.execute("BEGIN IMMEDIATE")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {"shadow_target_binding"}:
                raise RuntimeError("Shadow target store binding is invalid")
            previous = db.execute("SELECT generation,target_path,actor,source_binding,identity FROM shadow_target_binding").fetchone() if tables else None
            if previous is None:
                if not created:
                    raise RuntimeError("Shadow target store binding is missing; choose a new empty destination")
                owned_files = {self.target_marker_name,
                               self.target_marker_name + "-journal",
                               self.target_marker_name + "-wal",
                               self.target_marker_name + "-shm"}
                if any(path.name not in owned_files for path in self.target_path.iterdir()):
                    raise RuntimeError("Shadow target store is unbound and nonempty; choose a new empty destination")
                db.execute("""CREATE TABLE shadow_target_binding (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation TEXT NOT NULL,
                    target_path TEXT NOT NULL, actor TEXT NOT NULL, source_binding TEXT NOT NULL,
                    identity TEXT NOT NULL)""")
            if previous is not None and previous != self.values:
                raise RuntimeError("Shadow target store belongs to another actor/generation/source/identity; choose a separate destination")
            db.execute("INSERT OR IGNORE INTO shadow_target_binding VALUES(1,?,?,?,?,?)", self.values)

    def assert_destination(self, path):
        if path is None or Path(path).resolve() != self.target_path:
            raise RuntimeError("Shadow adapter destination differs from its bound target store")
        marker = self.target_path / self.target_marker_name
        with sqlite3.connect(marker.as_uri() + "?mode=ro", uri=True, timeout=30) as db:
            previous = db.execute("SELECT generation,target_path,actor,source_binding,identity FROM shadow_target_binding").fetchone()
        if previous != self.values:
            raise RuntimeError("Shadow target store binding changed")

    def assert_scope(self, scope):
        if str(scope.user_id) != self.actor or scope.project_id is not None or scope.space_id is not None:
            # CLI rehearsals use the bound actor's complete current scope.
            # Narrowing that same actor's next reconcile would also make its
            # library-wide stale deletion remove an earlier broader subset.
            raise RuntimeError("Shadow adapter scope must match the bound actor's complete scope")
        self.assert_destination(self.target_path)

    def units(self, ref, collection, points, *, completed, total=None):
        with self.ledger.connect() as db:
            db.executemany("INSERT OR REPLACE INTO shadow_units VALUES(?,?,?,?,?)", [
                (collection, ref.key, str(point.id), point.payload["content_hash"],
                 point.payload["embedding_fingerprint"]) for point in points
            ])
        self.ledger.mark(ref, "running", completed=completed, total=total)

    def finish(self, ref, collection, retained, *, verified_hash=None):
        # Remove metadata only after all destination units have been verified.
        with self.ledger.connect() as db:
            old = [row[0] for row in db.execute(
                "SELECT unit_id FROM shadow_units WHERE collection_name=? AND source_key=?",
                (collection, ref.key))]
            db.executemany("DELETE FROM shadow_units WHERE collection_name=? AND source_key=? AND unit_id=?",
                           [(collection, ref.key, unit_id) for unit_id in old if unit_id not in retained])
        self.ledger.mark(ref, "ready", completed=len(retained), total=len(retained), verified_hash=verified_hash)


class _ProjectedRowsSession:
    """Read-only row snapshots, deliberately without an ORM sync_session."""

    def __init__(self, rows):
        self.rows = rows

    async def execute(self, statement):
        if not statement.is_select:
            raise RuntimeError("Shadow row adapter only accepts reads")
        return SimpleNamespace(all=lambda: self.rows)


def _columns(model, prefix, names):
    return [getattr(model, name).label(prefix + name.lstrip("_")) for name in names]


async def _knowledge_rows(session, source_id):
    chunk_names = ["id", "_text", "heading_path", "chunk_index", "content_hash", "created_at"]
    doc_names = ["id", "title", "path", "extension", "status", "tags", "project_refs", "task_refs",
                 "created_at", "updated_at", "modified_at", "document_date", "document_date_source", "last_indexed_at"]
    source_names = ["id", "name", "source_type", "created_at", "updated_at", "last_synced_at"]
    statement = select(*_columns(KnowledgeChunk, "chunk_", chunk_names),
                       *_columns(KnowledgeDocument, "document_", doc_names),
                       *_columns(KnowledgeSource, "source_", source_names)).join(
        KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id).join(
        KnowledgeSource, KnowledgeDocument.source_id == KnowledgeSource.id).where(
        KnowledgeDocument.source_id == source_id, KnowledgeDocument.status == "active").order_by(
        KnowledgeDocument.path, KnowledgeChunk.chunk_index).limit(MAX_SHADOW_RECORDS + 1)
    rows = (await session.execute(statement)).all()
    if len(rows) > MAX_SHADOW_RECORDS:
        raise RuntimeError(f"Shadow source exceeds {MAX_SHADOW_RECORDS} records; partition the source")
    snapshots = []
    for row in rows:
        groups = []
        for prefix, names in [("chunk_", chunk_names), ("document_", doc_names), ("source_", source_names)]:
            groups.append(SimpleNamespace(**{name.lstrip("_"): row._mapping[prefix + name.lstrip("_")] for name in names}))
        groups[0].text = decrypt_text_if_needed(groups[0].text, aad="knowledge_chunks.text") or ""
        snapshots.append(tuple(groups))
    return snapshots


class KnowledgeShadowIndex(KnowledgeIndexService):
    """Canonical Knowledge batching, with detached projected metadata only."""

    def __init__(self, config, checkpoint):
        checkpoint.assert_destination(config.qdrant.local_path)
        super().__init__(config)
        self.checkpoint = checkpoint
        self._shadow_ref = None
        self._completed = 0
        self._total = 0

    async def _initialize(self):
        if self._initialized:
            return True
        if not self.config.qdrant.local_path:
            raise RuntimeError("Shadow Knowledge requires an isolated local destination")
        if not await self.embedding.initialize():
            raise RuntimeError("Shadow embedding unavailable")
        self._is_local_mode = True
        self.client = await self._index_io(SharedQdrantClient.get_client, self.config.qdrant.local_path)
        await self._index_io(self._ensure_collection)
        self._initialized = True
        return True

    async def _index_io(self, operation, *args, **kwargs):
        result = await super()._index_io(operation, *args, **kwargs)
        if self._shadow_ref is not None and getattr(operation, "__name__", "") == "upsert":
            points = kwargs["points"]
            self._completed += len(points)
            await run_index_io(self.checkpoint.units, self._shadow_ref, self.collection_name, points,
                               completed=self._completed, total=self._total)
        return result

    async def sync_shadow(self, session, resolver, source_id):
        self.checkpoint.assert_scope(resolver.scope)
        ref = SourceRef("knowledge_source", str(source_id))
        actor = await resolver._actor()
        if source_id not in await resolver._acl_knowledge_sources([source_id], actor):
            raise SourceUnavailable()
        rows = await _knowledge_rows(session, source_id)
        self._shadow_ref, self._completed, self._total = ref, 0, len(rows)
        self.checkpoint.ledger.mark(ref, "running", total=len(rows))
        try:
            # vector_id assignments affect SimpleNamespace copies, never live
            # ORM rows. No flush/commit/aftercommit media hook exists here.
            result = await self.sync_source(_ProjectedRowsSession(rows), source_id)
            if result.status != "synced":
                raise RuntimeError(result.error or result.status)
            if source_id not in await resolver._acl_knowledge_sources([source_id], await resolver._actor()):
                raise SourceUnavailable()
            current_rows = await _knowledge_rows(session, source_id)
            if {str(row[0].id): self._index_content_hash(*row) for row in current_rows} != {
                str(row[0].id): self._index_content_hash(*row) for row in rows
            }:
                raise RuntimeError("Knowledge source changed during shadow migration; rerun")
            persisted = await super()._index_io(self._source_points, source_id)
            for row in rows:
                point = persisted.get(str(row[0].id))
                if point is None or not self._valid_dense_vector(point.vector.get(self.dense_vector_name)) or (
                    point.payload.get("content_hash") != self._index_content_hash(*row)
                    or point.payload.get("embedding_fingerprint") != self._embedding_fingerprint()
                ):
                    raise RuntimeError("Shadow Knowledge persisted vector verification failed")
            self.checkpoint.finish(ref, self.collection_name, {str(row[0].id) for row in rows})
            return result.to_dict()
        finally:
            self._shadow_ref = None


class DocsShadowIndex(DocsIndexService):
    """Read canonical own-record projections; checkpoint every verified batch."""

    def __init__(self, config, checkpoint):
        checkpoint.assert_destination(config.qdrant.local_path)
        super().__init__(config)
        self.checkpoint = checkpoint

    async def _records(self, session, rows, library_id):
        ids = [row.id for row in rows]
        tags = {}
        tag_rows = (await session.execute(select(KnowledgeNodeSupertag.node_id, KnowledgeSupertag.name).join(
            KnowledgeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id).where(
            KnowledgeNodeSupertag.node_id.in_(ids), KnowledgeSupertag.docs_library_id == library_id))).all()
        for node_id, name in tag_rows:
            tags.setdefault(node_id, []).append(name)
        fields = await self._record_fields(session, library_id, ids)
        result = []
        for row in rows:
            node = SimpleNamespace(id=row.id, title=row.title, project_id=row.project_id, docs_library_id=library_id)
            own_tags = tags.get(row.id, [])
            body = decrypt_text_if_needed(row.body_text, aad="knowledge_nodes.body_text") or ""
            body_json = decrypt_json_value_if_needed(row.body_json, aad="knowledge_nodes.body_json")
            own_text = _node_text(row.title or "", [], own_tags, row.description or "", body, body_json, fields.get(row.id, ""))
            result.append((node, own_text, own_tags))
        return result

    async def sync_shadow(self, session, resolver, library_id):
        from qdrant_client import models

        self.checkpoint.assert_scope(resolver.scope)
        ref = SourceRef("docs_library", str(library_id))
        await resolver._actor()
        if not self.config.qdrant.local_path:
            raise RuntimeError("Shadow Docs requires an isolated local destination")
        if not await self.embedding.initialize():
            raise RuntimeError("Shadow embedding unavailable")
        self._is_local_mode = True
        self.client = await self._index_io(SharedQdrantClient.get_client, self.config.qdrant.local_path)
        await self._index_io(self._ensure_collection)
        self._initialized = True
        # Exactly the runtime search's canonical ACL/render-guard relation.
        statement, _ = await DocsGraphService(session)._build_structured_query_statement(
            docs_library_id=library_id, user_id=resolver.scope.user_id,
            project_id=resolver.scope.project_id)
        from ..services.docs_acl import apply_docs_search_visibility

        statement = apply_docs_search_visibility(statement, user_id=resolver.scope.user_id)
        columns = [KnowledgeNode.id, KnowledgeNode.title, KnowledgeNode.description, KnowledgeNode.project_id,
                   KnowledgeNode._body_text.label("body_text"), KnowledgeNode._body_json.label("body_json")]
        statement = statement.with_only_columns(*columns, maintain_column_froms=True).order_by(KnowledgeNode.id)
        batch_size = max(1, min(int(self.config.indexing.batch_size), int(self.config.embedding.batch_size), 32))
        cursor, completed, records, pending = None, 0, 0, []
        retained, record_hashes = set(), {}
        self.checkpoint.ledger.mark(ref, "running")

        async def recheck_pending():
            await resolver._actor()
            expected = {node.id: digest for node, _, _, _, _, digest in pending}
            rows = (await session.execute(statement.where(KnowledgeNode.id.in_(expected)))).all()
            current = {node.id: _content_hash(own_text)
                       for node, own_text, _ in await self._records(session, rows, library_id)}
            if current != expected:
                raise RuntimeError("Docs source/ACL changed during shadow batch; rerun")

        async def flush():
            nonlocal completed
            if not pending:
                return
            # A page can span many encoder batches. Refresh the same current
            # authorized original relation before sending text to the worker.
            await recheck_pending()
            existing = await self._index_io(self.client.retrieve, self.collection_name,
                                           [item[1] for item in pending], with_vectors=True, with_payload=True)
            existing = {str(point.id): point for point in existing}
            dense, changed = [], []
            for index, (_, point_id, _, _, _, digest) in enumerate(pending):
                previous = existing.get(point_id)
                vector = previous.vector.get("dense") if previous and isinstance(previous.vector, dict) else None
                payload = (previous.payload or {}) if previous else {}
                if (not KnowledgeIndexService._valid_dense_vector(self, vector) or payload.get("content_hash") != digest
                        or payload.get("embedding_fingerprint") != self._embedding_fingerprint()):
                    changed.append(index)
                    vector = None
                dense.append(vector)
            generated = await self.embedding.embed([pending[index][2] for index in changed],
                                                  titles=[pending[index][0].title for index in changed]) if changed else []
            if len(generated) != len(changed) or any(not KnowledgeIndexService._valid_dense_vector(self, v) for v in generated):
                raise RuntimeError("Invalid shadow Docs embeddings")
            for index, vector in zip(changed, generated):
                dense[index] = vector
            # Permissions/content may change while the worker is encoding or
            # while a stored prefix is being reused. Never ACK that old scope.
            await recheck_pending()
            points = []
            for (node, point_id, unit_text, offset, tags, digest), vector in zip(pending, dense):
                point = self._build_point(node, unit_text, tags, vector)
                point.id = point_id
                point.payload.update(content_hash=digest, unit_kind="record" if offset == 0 else "span", derived_text_offset=offset)
                points.append(point)
            await self._index_io(self.client.upsert, collection_name=self.collection_name, points=points, wait=True)
            verified = await self._index_io(self.client.retrieve, self.collection_name, [p.id for p in points],
                                           with_vectors=True, with_payload=True)
            expected = {str(p.id): p.payload["content_hash"] for p in points}
            if len(verified) != len(points) or any(
                not isinstance(p.vector, dict) or not KnowledgeIndexService._valid_dense_vector(self, p.vector.get("dense"))
                or p.payload.get("content_hash") != expected.get(str(p.id))
                or p.payload.get("embedding_fingerprint") != self._embedding_fingerprint() for p in verified
            ):
                raise RuntimeError("Shadow Docs persisted vector verification failed")
            completed += len(points)
            await run_index_io(self.checkpoint.units, ref, self.collection_name, points, completed=completed)
            retained.update(str(p.id) for p in points)
            pending.clear()

        while True:
            page = statement.limit(1000)
            if cursor is not None:
                page = page.where(KnowledgeNode.id > cursor)
            rows = (await session.execute(page)).all()
            if not rows:
                break
            records += len(rows)
            if records > MAX_SHADOW_RECORDS:
                raise RuntimeError(f"Shadow library exceeds {MAX_SHADOW_RECORDS} records; partition the library")
            for node, own_text, own_tags in await self._records(session, rows, library_id):
                digest = _content_hash(own_text)
                record_hashes[node.id] = digest
                for point_id, unit_text, offset in _node_units(node.id, node.title, own_text):
                    pending.append((node, point_id, unit_text, offset, own_tags, digest))
                    if len(pending) >= batch_size:
                        await flush()
            cursor = rows[-1].id
        await flush()
        await resolver._actor()
        # Current canonical eligibility is rechecked before any stale removal.
        await session.get(DocsLibrary, library_id, populate_existing=True)
        current, _ = await DocsGraphService(session)._build_structured_query_statement(
            docs_library_id=library_id, user_id=resolver.scope.user_id, project_id=resolver.scope.project_id)
        current = apply_docs_search_visibility(current, user_id=resolver.scope.user_id)
        current = current.with_only_columns(*columns, maintain_column_froms=True).order_by(KnowledgeNode.id)
        cursor, current_hashes = None, {}
        while True:
            page = current.limit(1000)
            if cursor is not None:
                page = page.where(KnowledgeNode.id > cursor)
            rows = (await session.execute(page)).all()
            if not rows:
                break
            for node, own_text, _ in await self._records(session, rows, library_id):
                current_hashes[node.id] = _content_hash(own_text)
            if len(current_hashes) > MAX_SHADOW_RECORDS:
                raise RuntimeError(f"Shadow library exceeds {MAX_SHADOW_RECORDS} records; partition the library")
            cursor = rows[-1].id
        if current_hashes != record_hashes:
            raise RuntimeError("Docs eligibility changed during shadow migration; rerun")
        stale, offset = [], None
        while True:
            old, offset = await self._index_io(self.client.scroll, collection_name=self.collection_name,
                scroll_filter=self._library_scan_filter(library_id), limit=1000, offset=offset, with_vectors=False)
            stale.extend(str(point.id) for point in old if str(point.id) not in retained)
            if offset is None:
                break
        for offset in range(0, len(stale), 1000):
            await self._index_io(self.client.delete, collection_name=self.collection_name,
                                points_selector=models.PointIdsList(points=stale[offset:offset + 1000]), wait=True)
        self.checkpoint.finish(ref, self.collection_name, retained)
        return {"status": "synced", "total": records, "indexed_units": completed, "removed": len(stale)}
