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
from dataclasses import dataclass, field
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
from ..services.embedding_feature import runtime_embedding_enabled
from .config import RagConfig, get_rag_config
from .docs_search_telemetry import DocsIndexSearchTelemetry
from .embedding import EmbeddingGemma2Embedding, get_embedding
from .qdrant_client import _LOCAL_INDEX_IO_LOCK, SharedQdrantClient
from .index_io import run_index_io

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

from ..rag.embedding_identity import EMBEDDING_FINGERPRINT, EMBEDDING_IDENTITY, embedding_collection_name

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
    # Master semantic switch first: OFF returns before config resolution, model
    # load or index/Qdrant initialization. ON keeps the docs gate below as-is.
    if not runtime_embedding_enabled():
        return False
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
    return hashlib.sha256(json.dumps([EMBEDDING_IDENTITY, model, dimension, DOCS_INDEX_INPUT_VERSION],
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
        self.collection_name = embedding_collection_name(getattr(self.config, "docs_collection_name", "aoitalk_docs"))
        self.client: Optional[QdrantClient] = None
        self.embedding = get_embedding(self.config.embedding)
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
        if not docs_rag_enabled(self.config):
            return False
        if self._initialized:
            return True
        if self._disabled:
            return False
        return await self._do_initialize()

    async def initialize_for_reindex(self) -> bool:
        """Reindex path: retry initialization after a bounded backoff."""
        if not docs_rag_enabled(self.config):
            return False
        if self._initialized:
            return True
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
        payload_schema = {}
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
            payload_schema = info.payload_schema or {}

        if not self._is_local_mode:
            self._ensure_payload_indexes(payload_schema)

    def _ensure_payload_indexes(self, payload_schema) -> None:
        """Fill missing server-side filter indexes without migrating data.

        Run for existing collections too. Embedded Qdrant ignores payload
        indexes; the caller skips this work in local mode. These indexes are
        optional accelerators, never a replacement for search/ACL filters.
        """
        fields = {
            "docs_library_id": models.PayloadSchemaType.KEYWORD,
            "node_id": models.PayloadSchemaType.KEYWORD,
            "project_id": models.PayloadSchemaType.KEYWORD,
            "input_version": models.PayloadSchemaType.INTEGER,
            "embedding_fingerprint": models.PayloadSchemaType.KEYWORD,
        }
        for field_name, field_schema in fields.items():
            existing = payload_schema.get(field_name)
            if existing is not None:
                if existing.data_type != field_schema:
                    logger.warning(
                        "Docs payload index %s has type %s, expected %s; retained",
                        field_name, existing.data_type, field_schema,
                    )
                continue
            try:
                self.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field_name,
                    field_schema=field_schema,
                    wait=True,
                )
            except Exception as exc:
                # Keep the usable collection and its original filter semantics
                # if index creation is unavailable (for example read-only access).
                logger.warning(
                    "Docs payload index could not be created: %s (%s)",
                    field_name, type(exc).__name__,
                )

    def _embedding_fingerprint(self):
        return docs_embedding_fingerprint(self.config.embedding.model, getattr(self.embedding, "dimension", None))

    async def _index_io(self, operation, *args, **kwargs):
        return await run_index_io(
            operation, *args,
            lock=_LOCAL_INDEX_IO_LOCK if self._is_local_mode else None,
            **kwargs,
        )

    # -- indexing -----------------------------------------------------------

    async def _upsert_points(self, points: list) -> None:
        if not points or self.client is None:
            return
        # Re-check at the write boundary as master OFF can land after a worker
        # acquired a session or finished an embedding request.
        if not docs_rag_enabled(self.config):
            raise DocsIndexUnavailable("Docs semantic index is disabled")
        await self._index_io(
            self.client.upsert,
            collection_name=self.collection_name,
            points=points,
        )

    async def _delete_ids(self, node_ids: list[uuid.UUID | str]) -> None:
        if not node_ids or self.client is None or models is None:
            return
        if not docs_rag_enabled(self.config):
            raise DocsIndexUnavailable("Docs semantic index is disabled")
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

    def _valid_dense_vector(self, vector):
        return (isinstance(vector, list) and len(vector) == self.embedding.dimension
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in vector)
                and abs(sum(x * x for x in vector) - 1.0) <= .01)

    def _native_dense_cosine(self, query, point):
        """Return a finite cosine for a prepared query and named dense point.

        Hybrid Docs searches expose Qdrant's partition-local RRF score.  The
        stored dense vector is the only comparable score across partitions.
        Any missing, malformed, non-finite, or zero-norm vector fails closed
        by returning ``None``; the raw Qdrant score must never be substituted.
        """
        from .media_query import prepared_dense_cosine

        vector = getattr(point, "vector", None)
        if not isinstance(vector, dict):
            return None
        dimension = getattr(getattr(self, "embedding", None), "dimension", None)
        return prepared_dense_cosine(query, vector.get(self.dense_vector_name), dimension)

    @staticmethod
    def _saved_sparse_matches(saved, expected):
        return (saved is not None and expected is not None and saved.indices == expected.indices
                and len(saved.values) == len(expected.values)
                and all(math.isfinite(a) and abs(a - b) <= 1e-6 for a, b in zip(saved.values, expected.values)))

    async def _saved_points_mismatches(
        self, entries, *, compare_dense=False, stop_on_mismatch=False
    ):
        """Return source node IDs whose persisted units fail the physical gate.

        ``entries`` contains ``(node_id, expected_point)`` pairs.  Keeping the
        source ID beside each point lets reconciliation combine units from
        several unchanged nodes into one bounded retrieve while still sending
        only the affected node back through the embedding/upsert path.
        """
        mismatched: set[str | None] = set()
        for start in range(0, len(entries), 32):
            batch = entries[start:start + 32]
            expected_counts: dict[str, int] = {}
            expected_nodes_by_id: dict[str, set[str | None]] = {}
            expected: dict[str, tuple[str | None, object]] = {}
            for node_id, point in batch:
                point_id = str(point.id)
                expected_counts[point_id] = expected_counts.get(point_id, 0) + 1
                expected_nodes_by_id.setdefault(point_id, set()).add(node_id)
                expected[point_id] = (node_id, point)
            saved = await self._index_io(
                self.client.retrieve,
                self.collection_name,
                [str(point.id) for _node_id, point in batch],
                with_vectors=True,
                with_payload=True,
            )
            saved_ids = [str(point.id) for point in saved]
            saved_counts: dict[str, int] = {}
            for point_id in saved_ids:
                saved_counts[point_id] = saved_counts.get(point_id, 0) + 1

            # A retrieve response is valid only when every requested ID occurs
            # exactly once and no extra ID appears.  Missing/duplicate IDs can
            # be attributed precisely; an unscoped extra makes the whole batch
            # suspect because it cannot be safely assigned to one source node.
            expected_nodes = {node_id for node_id, _point in batch}
            for point_id, (node_id, _point) in expected.items():
                if expected_counts[point_id] != 1:
                    mismatched.update(expected_nodes_by_id[point_id])
                elif saved_counts.get(point_id, 0) != 1:
                    mismatched.add(node_id)
            extra_ids = set(saved_counts) - set(expected)
            if extra_ids:
                extra_nodes = {
                    str((point.payload or {}).get("node_id"))
                    for point in saved
                    if str(point.id) in extra_ids
                    and isinstance(getattr(point, "payload", None), dict)
                    and (point.payload or {}).get("node_id")
                }
                mapped_extra_nodes = extra_nodes & {
                    str(node_id) for node_id in expected_nodes if node_id is not None
                }
                mismatched.update(mapped_extra_nodes or expected_nodes)

            for point in saved:
                current_entry = expected.get(str(point.id))
                if current_entry is None:
                    continue
                node_id, current = current_entry
                if (not isinstance(point.vector, dict)
                        or not self._valid_dense_vector(point.vector.get("dense"))
                        or point.payload != current.payload
                        or not self._saved_sparse_matches(
                            point.vector.get("sparse"), current.vector.get("sparse")
                        )):
                    mismatched.add(node_id)
                    continue
                if compare_dense:
                    vector = current.vector["dense"]
                    norm = math.sqrt(sum(x * x for x in vector))
                    if max(abs(a - b / norm) for a, b in zip(point.vector["dense"], vector)) > 1e-5:
                        mismatched.add(node_id)
            if stop_on_mismatch and mismatched:
                return mismatched
        return mismatched

    async def _saved_points_match(self, points, *, compare_dense=False):
        """Bounded persistence gate for both newly written and reused units."""
        entries = [(None, point) for point in points]
        return not await self._saved_points_mismatches(
            entries, compare_dense=compare_dense, stop_on_mismatch=True
        )

    def _saved_record_points(self, node, text, tags):
        manifest_hash = _content_hash(text)
        for point_id, unit_text, offset in _node_units(node.id, node.title, text):
            point = self._build_point(
                node,
                unit_text,
                tags,
                [1.] + [0.] * (self.embedding.dimension - 1),
            )
            point.id = point_id
            point.payload.update(
                content_hash=manifest_hash,
                unit_kind="record" if offset == 0 else "span",
                derived_text_offset=offset,
            )
            yield point

    async def _saved_records_mismatches(self, records):
        """Verify unchanged records with bounded cross-node retrieve batches."""
        mismatched: set[str] = set()
        entries = []

        async def flush():
            if not entries:
                return
            mismatched.update(await self._saved_points_mismatches(entries))
            entries.clear()

        for node, text, tags in records:
            node_id = str(node.id)
            if node_id in mismatched:
                continue
            for point in self._saved_record_points(node, text, tags):
                if node_id in mismatched:
                    break
                entries.append((node_id, point))
                if len(entries) >= 32:
                    await flush()
        await flush()
        return mismatched

    async def _saved_record_matches(self, node, text, tags):
        return not await self._saved_records_mismatches([(node, text, tags)])

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
        rows = (await session.execute(statement.execution_options(populate_existing=True))).all()
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
            vectors = await self.embedding.embed([entry[2] for entry in pending], titles=[entry[0].title for entry in pending])
            if len(vectors) != len(pending) or any(not self._valid_dense_vector(vector) for vector in vectors):
                raise DocsIndexUnavailable("Invalid Docs embeddings; durable queue retained")
            points = []
            for (node, point_id, unit_text, offset, tags, manifest_hash), vector in zip(pending, vectors):
                point = self._build_point(node, unit_text, tags, vector)
                point.id = point_id
                point.payload.update(content_hash=manifest_hash, unit_kind="record" if offset == 0 else "span",
                                     derived_text_offset=offset)
                points.append(point)
            await self._upsert_points(points)
            if not await self._saved_points_match(points, compare_dense=True):
                raise DocsIndexUnavailable("Docs persisted vector verification failed; durable queue retained")
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
        unchanged_records: list[tuple[KnowledgeNode, str, list[str]]] = []
        unchanged_units = 0
        expected_by_node = {}

        async def flush_unchanged_records():
            nonlocal unchanged_units
            if not unchanged_records:
                return
            mismatched = await self._saved_records_mismatches(unchanged_records)
            to_index.extend(
                record for record in unchanged_records if str(record[0].id) in mismatched
            )
            unchanged_records.clear()
            unchanged_units = 0

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
            node = KnowledgeNode(id=row.id, docs_library_id=docs_library_id, title=row.title, project_id=row.project_id)
            # A legacy payload must be upserted even when its hash is current,
            # so the point gets the canonical ``docs_library_id`` key and no
            # old ``workspace_id`` field remains in the current DTO.
            if (
                existing.get(str(row.id)) == _content_hash(text)
                and str(row.id) not in legacy_nodes
                and node_point_ids == expected
            ):
                unchanged_records.append((node, text, tags))
                unchanged_units += len(expected)
                if unchanged_units >= 32:
                    await flush_unchanged_records()
                continue
            await flush_unchanged_records()
            to_index.append((node, text, tags))

        await flush_unchanged_records()

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
        _prepared=None,
        _non_project: bool = False,
    ) -> "DocsIndexSearchResult":
        started = time.perf_counter()
        if docs_library_id is None:
            return self._empty_search_result(fallback_reason="missing_library")
        query = str(query or "").strip()
        if not query and _prepared is None:
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
                if _prepared is not None:
                    from ..memory.models import DocsLibrary
                    await session.get(DocsLibrary, docs_library_id, populate_existing=True)

                statement, _ = await DocsGraphService(session)._build_structured_query_statement(
                    docs_library_id=docs_library_id, project_id=project_id,
                    user_id=user_id, node_ids=scope_ids, tags=[tag] if tag else [],
                    turn_project_id=turn_project_id,
                )
                from ..services.docs_acl import apply_docs_search_visibility

                statement = apply_docs_search_visibility(statement, user_id=user_id)
                if _non_project:
                    statement = statement.where(KnowledgeNode.project_id.is_(None))
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

        prepared = _prepared is not None
        if prepared:
            from .media_query import require_prepared
            dense = require_prepared(_prepared)
            # Prepared semantic queries are intentionally dense-only.  The
            # lexical/hash sparse lane belongs to legacy/direct search and
            # must not alter the semantic candidate set or ranking.
            sparse = None
        else:
            dense = await self.embedding.embed_query(query)
            sparse = self.sparse_encoder.encode(query)
        dense_used, sparse_used = bool(dense), bool(sparse and sparse.indices)
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
        evidence = {}
        seen: set[uuid.UUID] = set()
        exhausted = False
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
                               limit=search_limit, with_payload=["node_id", "content_hash", "derived_text_offset"])
                if prepared:
                    # Prepared semantic queries are queried directly against
                    # the named dense vector.  Request the stored dense vector
                    # so the caller receives a real cosine rather than a
                    # partition-local Qdrant score.
                    options["with_vectors"] = [self.dense_vector_name]
                if len(prefetch) == 1:
                    options.update(query=prefetch[0].query, using=prefetch[0].using)
                else:
                    options.update(prefetch=prefetch, query=models.FusionQuery(fusion=models.Fusion.RRF))
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
                if _prepared is not None:
                    records = await self._prepared_originals(session, docs_library_id, live, user_id)
                    for point in response_points:
                        nid = uuid.UUID(str((point.payload or {}).get("node_id")))
                        record = records.get(nid)
                        if record is None or nid in evidence or point.payload.get("content_hash") != _content_hash(record[1]):
                            continue
                        node, text = record
                        start = int(point.payload.get("derived_text_offset") or 0)
                        body_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
                        hit = {"node_id": str(nid), "docs_library_id": str(docs_library_id),
                            "title": node.title, "text": text[start:start + 2000],
                            "content_hash": body_sha256, "body_sha256": body_sha256,
                            "_query_content_hash": _content_hash(text),
                            "citation": {"derived_text_start": start, "derived_text_end": min(start + 2000, len(text))}}
                        if prepared:
                            score = self._native_dense_cosine(dense, point)
                            if score is None:
                                # Prepared fusion requires a comparable native
                                # score.  Do not publish an unrankable hit or
                                # silently fall back to partition order.
                                raise ValueError("native_score_unavailable")
                            # Internal fusion metadata is stripped before the
                            # result crosses the API boundary.
                            hit["_query_semantic_score"] = score
                        evidence[nid] = hit
                    hits.extend(nid for nid in candidates if nid in live and nid in evidence)
                else:
                    hits.extend(nid for nid in candidates if nid in live)
                if len(hits) >= search_limit or (len(response_points) < options["limit"] and fusion != "rrf"):
                    exhausted = True
                    break
        except Exception:
            logger.debug("Docs index search failed", exc_info=True)
            return self._empty_search_result(fallback_reason="search_failed")
        return DocsIndexSearchResult(
            node_ids=hits[:search_limit],
            hits=[evidence[nid] for nid in hits[:search_limit]] if _prepared is not None else [],
            telemetry=DocsIndexSearchTelemetry(
                dense_used=dense_used, sparse_used=sparse_used, fusion=fusion,
                candidate_count=len(seen), latency_ms=(time.perf_counter() - started) * 1000.0,
                fallback_reason=None if exhausted else "candidate_budget",
            ),
        )

    async def _prepared_originals(self, session, library_id, node_ids, user_id):
        """Hydrate only currently eligible own-record text; never parents."""
        if session is None:
            raise PermissionError("prepared_docs_require_acl_session")
        if not node_ids:
            return {}
        nodes = (await session.execute(select(KnowledgeNode).where(KnowledgeNode.id.in_(node_ids),
            KnowledgeNode.docs_library_id == library_id).execution_options(populate_existing=True))).scalars().all()
        from ..services.docs_acl import can_read_node
        nodes = [node for node in nodes if await can_read_node(session, node.id, user_id)]
        node_ids = {node.id for node in nodes}
        if not node_ids:
            return {}
        tags = {}
        tag_rows = await session.execute(select(KnowledgeNodeSupertag.node_id, KnowledgeSupertag.name)
            .join(KnowledgeSupertag, KnowledgeSupertag.id == KnowledgeNodeSupertag.supertag_id)
            .where(KnowledgeNodeSupertag.node_id.in_(node_ids), KnowledgeSupertag.docs_library_id == library_id))
        for nid, name in tag_rows:
            tags.setdefault(nid, []).append(name)
        fields = await self._record_fields(session, library_id, node_ids)
        return {node.id: (node, _node_text(node.title, [], tags.get(node.id, []), node.description or "",
            node.body_text or "", node.body_json, fields.get(node.id, ""))) for node in nodes}

    async def search_prepared(self, unit, **scope):
        from .media_query import require_prepared
        require_prepared(unit)
        return await self.search(query=unit.lexical_text, _prepared=unit, **scope)

    async def authorize_prepared_hits(self, hits, *, session, docs_library_id, user_id,
                                      project_id=None, non_project=False):
        from ..memory.models import DocsLibrary
        from ..services.docs_graph_service import DocsGraphService
        from ..services.docs_acl import apply_docs_search_visibility
        await session.get(DocsLibrary, docs_library_id, populate_existing=True)
        identifiers = {uuid.UUID(hit["node_id"]) for hit in hits}
        statement, _ = await DocsGraphService(session)._build_structured_query_statement(
            docs_library_id=docs_library_id, user_id=user_id, project_id=project_id,
            turn_project_id=project_id, node_ids=identifiers)
        statement = apply_docs_search_visibility(statement, user_id=user_id)
        if non_project:
            statement = statement.where(KnowledgeNode.project_id.is_(None))
        legal = set((await session.execute(statement.with_only_columns(KnowledgeNode.id,
            maintain_column_froms=True))).scalars())
        originals = await self._prepared_originals(session, docs_library_id, legal, user_id)
        return [hit for hit in hits if uuid.UUID(hit["node_id"]) in originals and
            hit.get("_query_content_hash") == _content_hash(originals[uuid.UUID(hit["node_id"])][1])]


@dataclass(frozen=True)
class DocsIndexSearchResult:
    node_ids: list[uuid.UUID]
    telemetry: DocsIndexSearchTelemetry
    hits: list[dict] = field(default_factory=list)


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
    _semantic_enabled: bool | None = None,
) -> DocsIndexSearchResult:
    """Entry point used by ``docs_search`` with semantic-lane telemetry.

    ``_semantic_enabled`` is the request-sampled decision. ``False`` refuses
    before any index/Qdrant/embedding work; ``True`` only skips this entry
    re-sample, ``DocsIndexService.search`` and the embedding runtime still
    re-check the master switch. ``None`` (other callers) keeps the guard here.
    """
    if _semantic_enabled is None:
        _semantic_enabled = docs_rag_enabled()
    if docs_library_id is None or not _semantic_enabled:
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
    if not docs_rag_enabled():
        return False
    await _wait_reindex_worker_backoff()
    await asyncio.sleep(2.0)  # debounce a burst of edits
    if not _dirty or not docs_rag_enabled():
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
        if _dirty and docs_rag_enabled():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                _pending_without_loop.update(_dirty)
                return
            if not _worker_started:
                _worker_started = True
                loop.create_task(_drain_worker())
