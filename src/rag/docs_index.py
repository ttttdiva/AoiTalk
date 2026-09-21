"""Semantic index for the AoiTalk Docs graph (``KnowledgeNode``).

The Docs graph stores content as an outliner: each node is one short claim and
its "body" is its child nodes. The retrieval unit is a single node, embedded
using only its own content and same-library tags. Ancestors can have different
ACLs, so their text must never influence a readable descendant's vectors.

This index is derived and rebuildable. The canonical data is Postgres. Indexing
is controlled by ``rag.docs_enabled`` (default on). When disabled or unavailable,
``search_docs_index`` returns ``[]`` and ``docs_search`` falls back to the
lexical (DB) search.

Design notes:
- Collection ``rag.docs_collection_name`` (default ``aoitalk_docs``), separate
  from the Knowledge Workspace collection, with named dense + sparse vectors.
- Record point IDs use node IDs; long text adds deterministic span IDs.
  Search filters by payload node_id and collapses spans back to stable nodes.
- A per-node content hash (over the embedding input) lets ``reconcile_library``
  skip unchanged nodes. An input version forces migration of legacy contextual
  vectors; those points remain unsearchable until explicitly reconciled.
- Frontend Docs edits write straight to Postgres (bypassing the Python service),
  so ``reconcile_library`` is the catch-all sync; ``enqueue_docs_reindex`` gives
  near-real-time updates for backend/agent-originated edits.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    KnowledgeNode,
    KnowledgeField,
    KnowledgeFieldValue,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
)
from ..security.field_crypto import decrypt_json_value_if_needed, decrypt_text_if_needed
from ..services.docs_graph_service import docs_searchable_body_text
from .config import RagConfig, get_rag_config
from .docs_search_telemetry import DocsIndexSearchTelemetry
from .embedding import BgeM3Embedding
from .qdrant_client import _LOCAL_QUERY_LOCK, SharedQdrantClient

try:
    from qdrant_client import QdrantClient
    from qdrant_client.http import models

    QDRANT_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    QDRANT_AVAILABLE = False
    QdrantClient = None  # type: ignore[assignment]
    models = None  # type: ignore[assignment]

# Reuse the dependency-free sparse encoder (Japanese n-gram aware) from the
# Knowledge index so exact matches on tags / ids / model numbers stay findable.
from ..knowledge.index_service import HashingSparseEncoder

logger = logging.getLogger(__name__)


class DocsIndexUnavailable(RuntimeError):
    """Raised when targeted Docs indexing cannot reach the derived Qdrant index."""


REINDEX_INIT_RETRY_SECONDS = 30.0
REINDEX_WORKER_BACKOFF_INITIAL_SECONDS = 5.0
REINDEX_WORKER_BACKOFF_MAX_SECONDS = 60.0
DOCS_INDEX_INPUT_VERSION = 4
SEARCH_REPLENISH_ROUNDS = 4


class _LocalNodeIdValues(list[str]):
    """List-compatible, constant-time membership for embedded Qdrant.

    Its payload evaluator uses ``value in match.any`` for every point. A
    normal list makes an authorized corpus of N nodes take O(N**2) work.
    Retain the normal MatchAny/list wire shape, including deepcopy and JSON
    serialization, while indexing membership locally. These request-owned
    values are constructed once and never mutated.
    """

    def __init__(self, values: Iterable[str]) -> None:
        super().__init__(values)
        self._members = frozenset(self)

    def __contains__(self, value: object) -> bool:
        return isinstance(value, str) and value in self._members


def docs_rag_enabled(config: Optional[RagConfig] = None) -> bool:
    cfg = config or get_rag_config()
    return bool(getattr(cfg, "docs_enabled", False)) and QDRANT_AVAILABLE


def _node_text(
    title: str,
    path_titles: list[str],
    tags: list[str],
    description: str = "",
    body_text: str = "",
    body_json: object = None,
    fields_text: str = "",
) -> str:
    """Build the node-own text embedded for a node.

    ``path_titles`` is retained for compatibility with indexing callers but
    deliberately ignored: an ancestor can be unreadable to a node's reader.

    ``body_text`` is normally the title mirror and therefore does not need to
    be duplicated in the embedding input.  A typed Markdown/code block has
    independent editable content in ``body_json.content``; the shared helper
    selects that content so multiline source is available to both sparse and
    dense retrieval lanes.
    """
    parts: list[str] = []
    if tags:
        parts.append("tags: " + " ".join(f"#{t}" for t in sorted(tags)))
    title_text = str(title or "").strip()
    parts.append(title_text)
    body = docs_searchable_body_text(body_text, body_json).strip()
    if body and body != title_text:
        parts.append(body)
    if description:
        parts.append(str(description).strip())
    if fields_text:
        parts.append(fields_text)
    return "\n".join(p for p in parts if p)


def _content_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def docs_embedding_fingerprint(model: str, dimension: int | None) -> str:
    return hashlib.sha256(json.dumps([model, dimension, DOCS_INDEX_INPUT_VERSION],
                                    separators=(",", ":")).encode()).hexdigest()


def _node_units(node_id, title, text):
    """Rebuildable own-record spans; offsets belong to derived index text."""
    yield str(node_id), text[:2000], 0
    if len(text) > 2000:
        prefix = "title: " + str(title or "")[:300] + "\n"
        for start in range(1600, len(text), 1600):
            yield str(uuid.uuid5(node_id, f"docs-span-v4:{start}")), prefix + text[start:start + 2000], start


def _unit_ids(node_id, length):
    result = {str(node_id)}
    if length > 2000:
        result.update(str(uuid.uuid5(node_id, f"docs-span-v4:{start}")) for start in range(1600, length, 1600))
    return result


class DocsIndexService:
    """Synchronize and query the derived Qdrant Docs index."""

    dense_vector_name = "dense"
    sparse_vector_name = "sparse"

    def __init__(self, config: Optional[RagConfig] = None) -> None:
        self.config = config or get_rag_config()
        self.collection_name = getattr(self.config, "docs_collection_name", "aoitalk_docs")
        self.client: Optional[QdrantClient] = None
        self.embedding = BgeM3Embedding(self.config.embedding)
        self.sparse_encoder = HashingSparseEncoder()
        self._initialized = False
        self._is_local_mode = False
        # Once initialization fails (no model, no Qdrant, GPU OOM, ...), stay off
        # for the rest of the process so search silently falls back to lexical
        # instead of retrying the heavy model load on every call.
        self._disabled = False
        self._disabled_at: float | None = None

    def _mark_disabled(self) -> None:
        self._disabled = True
        self._disabled_at = time.monotonic()

    def _reindex_retry_allowed(self) -> bool:
        if not self._disabled:
            return True
        if self._disabled_at is None:
            return True
        return (time.monotonic() - self._disabled_at) >= REINDEX_INIT_RETRY_SECONDS

    async def initialize(self) -> bool:
        """Search/read path: stay off after a failed init for this process."""
        if self._initialized:
            return True
        if self._disabled or not docs_rag_enabled(self.config):
            return False
        return await self._do_initialize()

    async def initialize_for_reindex(self) -> bool:
        """Reindex path: retry initialization after a bounded backoff."""
        if self._initialized:
            return True
        if not docs_rag_enabled(self.config):
            return False
        if self._disabled:
            if not self._reindex_retry_allowed():
                return False
            self._disabled = False
        return await self._do_initialize()

    async def _do_initialize(self) -> bool:
        try:
            if not await self.embedding.initialize():
                logger.warning(
                    "Docs index: embedding model unavailable; disabling for this process"
                )
                self._mark_disabled()
                return False
            if self.config.qdrant.local_path:
                self._is_local_mode = True
                # Opening embedded Qdrant loads every persisted collection.
                # Keep this blocking startup work off the HTTP event loop,
                # under the same lock and cancellation rules as index I/O.
                self.client = await self._index_io(
                    SharedQdrantClient.get_client, self.config.qdrant.local_path
                )
            else:
                self.client = QdrantClient(
                    host=self.config.qdrant.host,
                    port=self.config.qdrant.port,
                    api_key=self.config.qdrant.api_key,
                )
                self._is_local_mode = False
            await self._index_io(self._ensure_collection)
            self._initialized = True
            self._disabled = False
            self._disabled_at = None
            return True
        except Exception:
            logger.exception("Docs index: failed to initialize; disabling for this process")
            self._mark_disabled()
            return False

    def _ensure_collection(self) -> None:
        if self.client is None or models is None:
            raise RuntimeError("Qdrant client is not initialized")
        names = {c.name for c in self.client.get_collections().collections}
        if self.collection_name not in names:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    self.dense_vector_name: models.VectorParams(
                        size=self.embedding.dimension,
                        distance=models.Distance.COSINE,
                    )
                },
                sparse_vectors_config={
                    self.sparse_vector_name: models.SparseVectorParams(
                        index=models.SparseIndexParams(on_disk=False)
                    )
                },
            )
        else:
            info = self.client.get_collection(self.collection_name)
            vectors = info.config.params.vectors
            dense = vectors.get(self.dense_vector_name) if isinstance(vectors, dict) else None
            sparse = info.config.params.sparse_vectors or {}
            if dense is None or dense.size != self.embedding.dimension or self.sparse_vector_name not in sparse:
                raise DocsIndexUnavailable("Incompatible Docs collection retained; use an explicitly migrated collection")

    def _embedding_fingerprint(self):
        return docs_embedding_fingerprint(self.config.embedding.model, getattr(self.embedding, "dimension", None))

    async def _index_io(self, operation, *args, **kwargs):
        from ..knowledge.index_service import _LOCAL_INDEX_IO_LOCK

        def invoke():
            if self._is_local_mode:
                with _LOCAL_INDEX_IO_LOCK:
                    return operation(*args, **kwargs)
            return operation(*args, **kwargs)
        pending = asyncio.get_running_loop().run_in_executor(None, invoke)
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not pending.cancelled():
                pending.exception()
            raise

    # -- indexing -----------------------------------------------------------

    async def _upsert_points(self, points: list) -> None:
        if not points or self.client is None:
            return
        guard = _LOCAL_QUERY_LOCK if self._is_local_mode else _NULL_GUARD
        async with guard:
            await self._index_io(
                self.client.upsert,
                collection_name=self.collection_name,
                points=points,
            )

    async def _delete_ids(self, node_ids: list[uuid.UUID | str]) -> None:
        if not node_ids or self.client is None or models is None:
            return
        guard = _LOCAL_QUERY_LOCK if self._is_local_mode else _NULL_GUARD
        async with guard:
            await self._index_io(
                self.client.delete,
                collection_name=self.collection_name,
                points_selector=models.PointIdsList(points=[str(nid) for nid in node_ids]),
            )

    def _build_point(self, node: KnowledgeNode, text: str, tags: list[str], dense: list[float]):
        docs_library_id = str(node.docs_library_id)
        return models.PointStruct(
            id=str(node.id),
            vector={
                self.dense_vector_name: dense,
                self.sparse_vector_name: self.sparse_encoder.encode(text),
            },
            payload={
                "node_id": str(node.id),
                "docs_library_id": docs_library_id,
                "project_id": str(node.project_id) if node.project_id else "",
                "title": node.title or "",
                "tags": tags,
                "input_version": DOCS_INDEX_INPUT_VERSION,
                "embedding_fingerprint": self._embedding_fingerprint(),
                "content_hash": _content_hash(text),
            },
        )

    @staticmethod
    def _library_filter(docs_library_id: uuid.UUID):
        """Scope Qdrant points to one canonical Docs Library."""

        value = str(docs_library_id)
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="docs_library_id", match=models.MatchValue(value=value)
                )
            ]
        )

    @staticmethod
    def _legacy_library_filter(docs_library_id: uuid.UUID):
        """Legacy ``workspace_id`` filter used only by reconciliation."""

        return models.FieldCondition(
            key="workspace_id", match=models.MatchValue(value=str(docs_library_id))
        )

    @classmethod
    def _library_scan_filter(cls, docs_library_id: uuid.UUID):
        """Scan canonical and pre-rename payloads at the reindex boundary.

        Search/query paths intentionally call :meth:`_library_filter` only;
        accepting ``workspace_id`` there would make an old payload a current
        API scope.  The reconciler is the one migration boundary allowed to
        dual-read and canonicalize those points.
        """

        return models.Filter(
            should=[
                cls._library_filter(docs_library_id).must[0],
                cls._legacy_library_filter(docs_library_id),
            ]
        )

    async def _existing_points(
        self, docs_library_id: uuid.UUID, node_ids=None
    ) -> tuple[dict[str, str], set[str], dict[str, set[str]]]:
        """Return hashes, legacy node IDs, and Qdrant point IDs by node.

        ``workspace_id`` is intentionally observed only here, at the explicit
        migration/reindex boundary.  A point's payload node ID is preferred;
        for old points that omitted it, the Qdrant point ID remains a stale
        candidate and will be removed during reconciliation.
        """

        if self.client is None or models is None:
            return {}, set(), {}
        hashes: dict[str, str] = {}
        legacy_nodes: set[str] = set()
        point_ids_by_node: dict[str, set[str]] = {}
        next_offset = None
        flt = self._library_scan_filter(docs_library_id)
        if node_ids is not None:
            values = list(map(str, node_ids))
            if not values:
                return {}, set(), {}
            flt = models.Filter(must=[flt, models.Filter(should=[
                models.FieldCondition(key="node_id", match=models.MatchAny(any=values)),
                models.HasIdCondition(has_id=values),
            ])])
        while True:
            records, next_offset = await self._index_io(
                self.client.scroll,
                collection_name=self.collection_name,
                scroll_filter=flt,
                limit=2000,
                with_payload=[
                    "node_id",
                    "content_hash",
                    "docs_library_id",
                    "workspace_id",
                    "input_version",
                    "embedding_fingerprint",
                    "text",
                ],
                with_vectors=False,
                offset=next_offset,
            )
            for record in records:
                payload = record.payload or {}
                point_id = str(getattr(record, "id", "") or "")
                node_id = payload.get("node_id")
                node_key = str(node_id) if node_id else ""
                if node_key:
                    point_hash = str(payload.get("content_hash") or "")
                    if node_key in hashes and hashes[node_key] != point_hash:
                        legacy_nodes.add(node_key)  # Interrupted partial replacement.
                    hashes[node_key] = point_hash
                    point_ids_by_node.setdefault(node_key, set()).add(point_id)
                    if (
                        payload.get("workspace_id")
                        or payload.get("input_version") != DOCS_INDEX_INPUT_VERSION
                        or payload.get("embedding_fingerprint") != self._embedding_fingerprint()
                        or "text" in payload
                    ):
                        legacy_nodes.add(node_key)
                elif point_id:
                    # Keep malformed points in a dedicated bucket so the
                    # caller can delete them as stale without coercing an
                    # arbitrary string into UUID.
                    point_ids_by_node.setdefault(f"__point__:{point_id}", set()).add(point_id)
            if next_offset is None:
                break
        return hashes, legacy_nodes, point_ids_by_node

    async def _existing_hashes(self, docs_library_id: uuid.UUID) -> dict[str, str]:
        """Return node_id -> content_hash for points already in the collection."""
        hashes, _legacy_nodes, _point_ids = await self._existing_points(docs_library_id)
        return hashes

    async def indexed_library_ids(self) -> set[uuid.UUID]:
        """Discover canonical/legacy library IDs present in Qdrant.

        This is intentionally a reindex-only operation.  It lets the
        reconciler clean points whose Postgres library/node was deleted even
        when no active DB node remains to seed the library loop.
        """

        if self.client is None or models is None:
            return set()
        ids: set[uuid.UUID] = set()
        next_offset = None
        while True:
            records, next_offset = await self._index_io(
                self.client.scroll,
                collection_name=self.collection_name,
                limit=2000,
                with_payload=["docs_library_id", "workspace_id"],
                with_vectors=False,
                offset=next_offset,
            )
            for record in records:
                payload = record.payload or {}
                for key in ("docs_library_id", "workspace_id"):
                    value = payload.get(key)
                    if not value:
                        continue
                    try:
                        ids.add(uuid.UUID(str(value)))
                    except (TypeError, ValueError, AttributeError):
                        continue
            if next_offset is None:
                break
        return ids

    async def _record_fields(self, session, library_id, node_ids=None):
        from ..services.docs_graph_service import DocsGraphService, TASK_FIELD_TO_TASK_UPDATE
        statement = select(KnowledgeField, KnowledgeFieldValue).join(
            KnowledgeFieldValue, KnowledgeFieldValue.field_id == KnowledgeField.id,
        ).join(KnowledgeNode, KnowledgeNode.id == KnowledgeFieldValue.node_id).where(
            KnowledgeNode.docs_library_id == library_id, KnowledgeNode.archived_at.is_(None),
            KnowledgeField.docs_library_id == library_id,
        ).order_by(KnowledgeFieldValue.node_id, KnowledgeField.id)
        if node_ids is not None:
            statement = statement.where(KnowledgeFieldValue.node_id.in_(node_ids))
        rows = (await session.execute(statement)).all()
        fields = {}
        formatter = DocsGraphService(session)
        for field, value in rows:
            # A share of a Docs node is not a grant to bound Task metadata or a
            # referenced node. Index only values owned by this record itself.
            if field.field_type == "reference" or field.system_key in TASK_FIELD_TO_TASK_UPDATE:
                continue
            rendered = formatter._format_field_value(field, value)
            if rendered:
                fields.setdefault(value.node_id, []).append(f"{field.name}: {rendered}")
        return {node_id: "\n".join(values) for node_id, values in fields.items()}

    async def _index_records(self, records: list[tuple[KnowledgeNode, str, list[str]]]) -> int:
        """Embed/upsert bounded batches rather than all library vectors at once."""
        batch_size = max(1, int(self.config.indexing.batch_size))
        pending = []
        indexed = 0
        async def flush():
            nonlocal indexed
            if not pending:
                return
            vectors = await self.embedding.embed([entry[2] for entry in pending])
            dimension = getattr(self.embedding, "dimension", None)
            if len(vectors) != len(pending) or any(
                not vector or (isinstance(dimension, int) and len(vector) != dimension)
                or any(not math.isfinite(float(value)) for value in vector) for vector in vectors
            ):
                raise DocsIndexUnavailable("Invalid Docs embeddings; durable queue retained")
            points = []
            for (node, point_id, unit_text, offset, tags, manifest_hash), vector in zip(pending, vectors):
                point = self._build_point(node, unit_text, tags, vector)
                point.id = point_id
                point.payload.update(content_hash=manifest_hash, unit_kind="record" if offset == 0 else "span",
                                     derived_text_offset=offset)
                points.append(point)
            await self._upsert_points(points)
            indexed += len(points)
            pending.clear()
        for node, text, tags in records:
            manifest_hash = _content_hash(text)
            for point_id, unit_text, offset in _node_units(node.id, node.title, text):
                pending.append((node, point_id, unit_text, offset, tags, manifest_hash))
                if len(pending) >= batch_size:
                    await flush()
        await flush()
        return indexed

    async def reconcile_library(self, session: AsyncSession, docs_library_id: uuid.UUID) -> dict:
        """Bring the index in line with Postgres for one workspace.

        Re-embeds nodes whose own content or input version changed and removes points for
        archived/deleted nodes. Safe to run on a schedule; this is the catch-all
        sync for edits made through the frontend (which bypasses Python).
        """
        from ..services.docs_consistency import lock_docs_index
        await lock_docs_index(session)
        if not await self.initialize():
            return {"status": "disabled"}

        rows = await session.execute(
            select(
                KnowledgeNode.id,
                KnowledgeNode.title,
                KnowledgeNode.description,
                KnowledgeNode.project_id,
                KnowledgeNode._body_text.label("body_text"),
                KnowledgeNode._body_json.label("body_json"),
            ).where(
                KnowledgeNode.docs_library_id == docs_library_id,
                KnowledgeNode.archived_at.is_(None),
            )
        )
        node_rows = list(rows.all())

        tag_rows = await session.execute(
            select(KnowledgeNodeSupertag.node_id, KnowledgeSupertag.name)
            .join(KnowledgeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
            .join(KnowledgeNode, KnowledgeNode.id == KnowledgeNodeSupertag.node_id)
            .where(
                KnowledgeNode.docs_library_id == docs_library_id,
                KnowledgeNode.archived_at.is_(None),
                # A malformed cross-library relation must not inject a
                # foreign tag name into this library's embedding text.
                KnowledgeSupertag.docs_library_id == docs_library_id,
            )
        )
        tags_by_id: dict[uuid.UUID, list[str]] = {}
        for node_id, tag_name in tag_rows.all():
            tags_by_id.setdefault(node_id, []).append(tag_name)

        record_fields = await self._record_fields(session, docs_library_id)
        existing, legacy_nodes, point_ids_by_node = await self._existing_points(docs_library_id)
        current_ids: set[str] = set()
        to_index: list[tuple[KnowledgeNode, str, list[str]]] = []
        expected_by_node = {}
        for row in node_rows:
            current_ids.add(str(row.id))
            tags = tags_by_id.get(row.id, [])
            body_text = decrypt_text_if_needed(
                getattr(row, "body_text", None),
                aad="knowledge_nodes.body_text",
            ) or ""
            body_json = decrypt_json_value_if_needed(
                getattr(row, "body_json", None),
                aad="knowledge_nodes.body_json",
            )
            text = _node_text(
                row.title or "",
                [],
                tags,
                row.description or "",
                body_text,
                body_json,
                record_fields.get(row.id, ""),
            )
            expected = _unit_ids(row.id, len(text))
            expected_by_node[str(row.id)] = expected
            node_point_ids = point_ids_by_node.get(str(row.id), set())
            # A legacy payload must be upserted even when its hash is current,
            # so the point gets the canonical ``docs_library_id`` key and no
            # old ``workspace_id`` field remains in the current DTO.
            if (
                existing.get(str(row.id)) == _content_hash(text)
                and str(row.id) not in legacy_nodes
                and node_point_ids == expected
            ):
                continue
            node = KnowledgeNode(
                id=row.id,
                docs_library_id=docs_library_id,
                title=row.title,
                project_id=row.project_id,
            )
            to_index.append((node, text, tags))

        indexed = await self._index_records(to_index)

        stale_point_ids: set[str] = set()
        for node_key, point_ids in point_ids_by_node.items():
            if node_key.startswith("__point__:") or node_key not in current_ids:
                stale_point_ids.update(point_ids)
            elif node_key in current_ids and point_ids != expected_by_node[node_key]:
                # Duplicate/legacy point IDs for a live node are removed after
                # the canonical point is upserted.
                stale_point_ids.update(point_ids - expected_by_node[node_key])
        await self._delete_ids(sorted(stale_point_ids))

        return {
            "status": "synced",
            "total": len(node_rows),
            "reindexed": len(to_index),
            "indexed_units": indexed,
            "removed": len(stale_point_ids),
        }

    async def index_node_ids(
        self,
        session: AsyncSession,
        docs_library_id: uuid.UUID | None = None,
        node_ids: list[uuid.UUID] | None = None,
    ) -> int:
        """Targeted (re)index for specific nodes (agent/backend-originated edits)."""
        if docs_library_id is None:
            return 0
        node_ids = node_ids or []
        if not node_ids:
            return 0
        from ..services.docs_consistency import lock_docs_index
        await lock_docs_index(session)
        if not await self.initialize_for_reindex():
            raise DocsIndexUnavailable("Docs semantic index is unavailable")
        record_fields = await self._record_fields(session, docs_library_id, node_ids)
        records: list[tuple[KnowledgeNode, str, list[str]]] = []
        remove: list[uuid.UUID] = []
        for node_id in node_ids:
            node = await session.get(KnowledgeNode, node_id)
            if node is None or node.docs_library_id != docs_library_id or node.archived_at is not None:
                remove.append(node_id)
                continue
            tag_rows = await session.execute(
                select(KnowledgeSupertag.name)
                .join(KnowledgeNodeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
                .where(
                    KnowledgeNodeSupertag.node_id == node.id,
                    KnowledgeSupertag.docs_library_id == docs_library_id,
                )
            )
            tags = [name for (name,) in tag_rows.all()]
            text = _node_text(
                node.title or "",
                [],
                tags,
                node.description or "",
                node.body_text or "",
                node.body_json,
                record_fields.get(node.id, ""),
            )
            records.append((node, text, tags))
        _, _, existing = await self._existing_points(docs_library_id, node_ids)
        indexed = await self._index_records(records)
        stale = set(map(str, remove))
        desired = {str(node.id): _unit_ids(node.id, len(text)) for node, text, _ in records}
        for node_key, point_ids in existing.items():
            stale.update(point_ids - desired.get(node_key, set()))
        await self._delete_ids(sorted(stale))
        return len(records)

    # -- search -------------------------------------------------------------

    def _node_id_condition(self, node_ids: Iterable[uuid.UUID]) -> models.FieldCondition:
        match = models.MatchAny(any=[str(nid) for nid in sorted(node_ids, key=str)])
        if self._is_local_mode:
            # Install after model validation, which otherwise normalizes a
            # list subclass back to an ordinary linear-membership list.
            match.any = _LocalNodeIdValues(match.any)
        return models.FieldCondition(key="node_id", match=match)

    @staticmethod
    def _empty_search_result(
        *,
        fallback_reason: str,
        latency_ms: float = 0.0,
        dense_used: bool = False,
        sparse_used: bool = False,
        fusion: str = "none",
    ) -> "DocsIndexSearchResult":
        return DocsIndexSearchResult(
            node_ids=[],
            telemetry=DocsIndexSearchTelemetry(
                dense_used=dense_used,
                sparse_used=sparse_used,
                fusion=fusion,
                candidate_count=0,
                latency_ms=latency_ms,
                fallback_reason=fallback_reason,
            ),
        )

    async def search(
        self,
        *,
        docs_library_id: uuid.UUID | None = None,
        query: str,
        project_id: Optional[uuid.UUID] = None,
        limit: int = 20,
        user_id: Optional[uuid.UUID] = None,
        session: Optional[AsyncSession] = None,
        allowed_node_ids: Iterable[uuid.UUID] | None = None,
        tag: str = "",
        turn_project_id: uuid.UUID | None = None,
    ) -> "DocsIndexSearchResult":
        started = time.perf_counter()
        if docs_library_id is None:
            return self._empty_search_result(fallback_reason="missing_library")
        query = str(query or "").strip()
        if not query:
            return self._empty_search_result(fallback_reason="empty_query")
        scope_ids = None if allowed_node_ids is None else set(allowed_node_ids)
        if scope_ids == set():
            return self._empty_search_result(fallback_reason="empty_scope")
        if (user_id is not None or scope_ids is not None or tag) and session is None:
            return self._empty_search_result(fallback_reason="acl_session_required")
        if scope_ids is not None and user_id is None:
            return self._empty_search_result(fallback_reason="scope_actor_required")

        try:
            if session is not None:
                from ..services.docs_graph_service import DocsGraphService

                statement, _ = await DocsGraphService(session)._build_structured_query_statement(
                    docs_library_id=docs_library_id, project_id=project_id,
                    user_id=user_id, node_ids=scope_ids, tags=[tag] if tag else [],
                    turn_project_id=turn_project_id,
                )
                eligibility = statement.with_only_columns(KnowledgeNode.id, maintain_column_froms=True)
                eligible_ids = set((await session.execute(eligibility)).scalars().all())
            else:
                eligibility, eligible_ids = None, None
        except Exception:
            logger.debug("Docs index eligibility failed", exc_info=True)
            return self._empty_search_result(fallback_reason="acl_filter_failed")
        if eligible_ids == set():
            return self._empty_search_result(fallback_reason="no_eligible_nodes")
        if not await self.initialize() or self.client is None or models is None:
            return self._empty_search_result(fallback_reason="index_unavailable")

        dense = await self.embedding.embed_query(query)
        sparse = self.sparse_encoder.encode(query)
        dense_used, sparse_used = bool(dense), bool(sparse.indices)
        if not dense_used and not sparse_used:
            return self._empty_search_result(fallback_reason="no_vectors")
        must = [
            self._library_filter(docs_library_id),
            models.FieldCondition(key="input_version", match=models.MatchValue(value=DOCS_INDEX_INPUT_VERSION)),
            models.FieldCondition(key="embedding_fingerprint", match=models.MatchValue(value=self._embedding_fingerprint())),
        ]
        if eligible_ids is not None:
            must.append(self._node_id_condition(eligible_ids))
        elif project_id is not None:
            must.append(models.FieldCondition(key="project_id", match=models.MatchValue(value=str(project_id))))
        search_limit = max(1, min(int(limit), 100))
        fusion = "rrf" if dense_used and sparse_used else "dense" if dense_used else "sparse"
        hits: list[uuid.UUID] = []
        seen: set[uuid.UUID] = set()
        exhausted = False
        guard = _LOCAL_QUERY_LOCK if self._is_local_mode else _NULL_GUARD
        try:
            for _ in range(SEARCH_REPLENISH_ROUNDS):
                query_filter = models.Filter(
                    must=must,
                    must_not=[self._node_id_condition(seen)] if seen else None,
                )
                prefetch = []
                if dense_used:
                    prefetch.append(models.Prefetch(query=dense, using=self.dense_vector_name,
                                                   filter=query_filter, limit=max(64, search_limit * 8)))
                if sparse_used:
                    prefetch.append(models.Prefetch(query=sparse, using=self.sparse_vector_name,
                                                   filter=query_filter, limit=max(64, search_limit * 8)))
                options = dict(collection_name=self.collection_name, query_filter=query_filter,
                               limit=search_limit, with_payload=["node_id"])
                if len(prefetch) == 1:
                    options.update(query=prefetch[0].query, using=prefetch[0].using)
                else:
                    options.update(prefetch=prefetch, query=models.FusionQuery(fusion=models.Fusion.RRF))
                async with guard:
                    grouped = getattr(self.client, "query_points_groups", None)
                    if callable(grouped) and not self._is_local_mode:
                        response = await self._index_io(grouped, group_by="node_id", group_size=1, **options)
                        response_points = [group.hits[0] for group in response.groups if group.hits]
                    else:
                        # Embedded Qdrant implements grouping by overriding
                        # every prefetch/return limit with the collection size.
                        # Use bounded candidates and deduplicate below; the next
                        # round excludes seen nodes, including all their spans.
                        if self._is_local_mode:
                            options["limit"] = max(64, search_limit * 8)
                        response = await self._index_io(self.client.query_points, **options)
                        response_points = response.points
                candidates = []
                for point in response_points:
                    try:
                        nid = uuid.UUID(str((point.payload or {}).get("node_id")))
                    except (ValueError, TypeError):
                        continue
                    if nid not in seen:
                        candidates.append(nid)
                        seen.add(nid)
                if not candidates:
                    exhausted = True
                    break
                # SQL IN does not preserve vector order. Intersect, then retain
                # the ordered Qdrant IDs, rechecking current ACL/tag/archive.
                live = (
                    set((await session.execute(eligibility.where(KnowledgeNode.id.in_(candidates)))).scalars().all())
                    if session is not None else set(candidates)
                )
                hits.extend(nid for nid in candidates if nid in live)
                if len(hits) >= search_limit or (len(response_points) < options["limit"] and fusion != "rrf"):
                    exhausted = True
                    break
        except Exception:
            logger.debug("Docs index search failed", exc_info=True)
            return self._empty_search_result(fallback_reason="search_failed")
        return DocsIndexSearchResult(
            node_ids=hits[:search_limit],
            telemetry=DocsIndexSearchTelemetry(
                dense_used=dense_used, sparse_used=sparse_used, fusion=fusion,
                candidate_count=len(seen), latency_ms=(time.perf_counter() - started) * 1000.0,
                fallback_reason=None if exhausted else "candidate_budget",
            ),
        )


@dataclass(frozen=True)
class DocsIndexSearchResult:
    node_ids: list[uuid.UUID]
    telemetry: DocsIndexSearchTelemetry


class _NullAsyncGuard:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


_NULL_GUARD = _NullAsyncGuard()


_docs_index_service: Optional[DocsIndexService] = None
_dirty: set[tuple[uuid.UUID, uuid.UUID]] = set()
_pending_without_loop: set[tuple[uuid.UUID, uuid.UUID]] = set()
_worker_started = False
_durable_worker_active = False
_reindex_worker_backoff_seconds = REINDEX_WORKER_BACKOFF_INITIAL_SECONDS
_reindex_worker_backoff_until = 0.0


def _reset_reindex_worker_backoff() -> None:
    global _reindex_worker_backoff_seconds, _reindex_worker_backoff_until
    _reindex_worker_backoff_seconds = REINDEX_WORKER_BACKOFF_INITIAL_SECONDS
    _reindex_worker_backoff_until = 0.0


def _schedule_reindex_worker_backoff() -> None:
    global _reindex_worker_backoff_seconds, _reindex_worker_backoff_until
    _reindex_worker_backoff_until = time.monotonic() + _reindex_worker_backoff_seconds
    _reindex_worker_backoff_seconds = min(
        _reindex_worker_backoff_seconds * 2,
        REINDEX_WORKER_BACKOFF_MAX_SECONDS,
    )


async def _wait_reindex_worker_backoff() -> None:
    delay = _reindex_worker_backoff_until - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)


def get_docs_index_service() -> DocsIndexService:
    global _docs_index_service
    if _docs_index_service is None:
        _docs_index_service = DocsIndexService()
    return _docs_index_service


async def search_docs_index_with_telemetry(
    *,
    docs_library_id: uuid.UUID | None = None,
    query: str,
    project_id: Optional[uuid.UUID] = None,
    limit: int = 20,
    user_id: Optional[uuid.UUID] = None,
    session: Optional[AsyncSession] = None,
    allowed_node_ids: Iterable[uuid.UUID] | None = None,
    tag: str = "",
    turn_project_id: uuid.UUID | None = None,
) -> DocsIndexSearchResult:
    """Entry point used by ``docs_search`` with semantic-lane telemetry."""
    if docs_library_id is None or not docs_rag_enabled():
        return DocsIndexService._empty_search_result(fallback_reason="disabled")
    try:
        return await get_docs_index_service().search(
            docs_library_id=docs_library_id,
            query=query,
            project_id=project_id,
            limit=limit,
            user_id=user_id,
            session=session,
            allowed_node_ids=allowed_node_ids,
            tag=tag,
            turn_project_id=turn_project_id,
        )
    except Exception:
        logger.debug("search_docs_index failed", exc_info=True)
        return DocsIndexService._empty_search_result(fallback_reason="search_failed")


async def search_docs_index(
    *,
    docs_library_id: uuid.UUID | None = None,
    query: str,
    project_id: Optional[uuid.UUID] = None,
    limit: int = 20,
    user_id: Optional[uuid.UUID] = None,
    session: Optional[AsyncSession] = None,
    allowed_node_ids: Iterable[uuid.UUID] | None = None,
    tag: str = "",
    turn_project_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Entry point used by ``docs_search``. Returns [] when disabled/unavailable."""
    result = await search_docs_index_with_telemetry(
        docs_library_id=docs_library_id,
        query=query,
        project_id=project_id,
        limit=limit,
        user_id=user_id,
        session=session,
        allowed_node_ids=allowed_node_ids,
        tag=tag,
        turn_project_id=turn_project_id,
    )
    return result.node_ids


def enqueue_docs_reindex(
    docs_library_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
) -> None:
    """Mark a node dirty and (best-effort) drain it in the background.

    Called from inside a Docs mutation transaction, so it must never raise and
    must be a cheap no-op when the Docs index is disabled.
    """
    if _durable_worker_active or docs_library_id is None or node_id is None:
        return
    if not docs_rag_enabled():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _pending_without_loop.add((docs_library_id, node_id))
        return
    _dirty.add((docs_library_id, node_id))
    global _worker_started
    if not _worker_started:
        _worker_started = True
        asyncio.get_running_loop().create_task(_drain_worker())


async def flush_pending_docs_reindex() -> None:
    """Flush enqueue requests made outside an asyncio loop and drain dirty nodes."""
    if _pending_without_loop:
        _dirty.update(_pending_without_loop)
        _pending_without_loop.clear()
    global _worker_started
    if not _dirty or _worker_started:
        return
    _worker_started = True
    await _drain_worker()


async def _drain_docs_reindex_once() -> bool:
    if not _dirty:
        return False
    await _wait_reindex_worker_backoff()
    await asyncio.sleep(2.0)  # debounce a burst of edits
    if not _dirty:
        return False
    pending = list(_dirty)
    _dirty.clear()
    by_library: dict[uuid.UUID, list[uuid.UUID]] = {}
    for docs_library_id, node_id in pending:
        by_library.setdefault(docs_library_id, []).append(node_id)
    from ..memory.database import get_database_manager

    db = get_database_manager()
    service = get_docs_index_service()
    failed: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for docs_library_id, node_ids in by_library.items():
        session = await db.get_session()
        try:
            await service.index_node_ids(session, docs_library_id, node_ids)
        except DocsIndexUnavailable:
            logger.debug("Docs reindex worker deferred: index unavailable", exc_info=True)
            for node_id in node_ids:
                failed.add((docs_library_id, node_id))
        except Exception:
            logger.debug("Docs reindex worker failed", exc_info=True)
            for node_id in node_ids:
                failed.add((docs_library_id, node_id))
        finally:
            await session.close()
    if failed:
        _dirty.update(failed)
        _schedule_reindex_worker_backoff()
        return bool(_dirty)
    _reset_reindex_worker_backoff()
    return bool(_dirty)


async def _drain_worker() -> None:
    global _worker_started
    try:
        while await _drain_docs_reindex_once():
            continue
    finally:
        _worker_started = False
        if _dirty:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                _pending_without_loop.update(_dirty)
                return
            if not _worker_started:
                _worker_started = True
                loop.create_task(_drain_worker())
