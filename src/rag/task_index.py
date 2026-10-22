"""Per-search semantic index for first-class Tasks.

Task rows are canonical PostgreSQL data.  The Qdrant collection in this
module is a rebuildable derivative used only to order candidates.  Every
candidate is re-read from PostgreSQL before it is returned, and the payload
contains identifiers and hashes only; task text never enters Qdrant payloads.

The task search tool deliberately synchronizes the readable project corpus
before querying.  This keeps the feature correct without a new database
outbox or a new public search lane.  Unchanged vectors are reused by their
content hash, while deleted/archived/purged points are withdrawn only inside
projects which are still readable by the current actor.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import math
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence
from uuid import UUID

from qdrant_client import QdrantClient, models
from sqlalchemy import select
from .config import RagConfig, get_rag_config
from .embedding import get_embedding
from .embedding_identity import (
    DIMENSION,
    EMBEDDING_FINGERPRINT,
    document_input,
    embedding_collection_name,
)
from .index_io import run_index_io
from .qdrant_client import SharedQdrantClient, _LOCAL_INDEX_IO_LOCK
from ..memory.models import Task, TaskAssignee


TASK_INDEX_COLLECTION = embedding_collection_name("aoitalk_tasks")
TASK_INDEX_PAGE_SIZE = 100
TASK_EMBED_BATCH_SIZE = 32
TASK_SEARCH_OVERFETCH = 8
TASK_SEARCH_MAX_POINTS = 400
TASK_INDEX_RACE_RETRIES = 1
TASK_INDEX_TIMEOUT_SECONDS = 300.0


class TaskIndexError(RuntimeError):
    """Sanitized failure which must never become an empty search result."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


class TaskIndexRace(TaskIndexError):
    def __init__(self):
        super().__init__("task_index_race")


class TaskIndexUnavailable(TaskIndexError):
    def __init__(self, code: str = "task_semantic_unavailable"):
        super().__init__(code)


@dataclass(frozen=True)
class _TaskSnapshot:
    id: str
    project_id: str
    title: str
    description: str
    status: str
    updated_at: Any
    deleted: bool
    archived: bool
    assignee_ids: tuple[str, ...]

    @property
    def input_text(self) -> str:
        return document_input(self.description, self.title)

    @property
    def content_hash(self) -> str:
        return task_content_hash(self.title, self.description)


@dataclass(frozen=True)
class TaskSearchHit:
    """Internal semantic hit carrying the query-time source fingerprint."""

    task_id: str
    content_hash: str
    score: float


def task_content_hash(title: str | None, description: str | None) -> str:
    value = document_input(str(description or ""), str(title or ""))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _semantic_enabled_from_runtime() -> bool:
    """Use the configured local search capability; never infer from imports."""

    try:
        from ..features import Features

        # Enterprise's current contract keeps the existing lexical Task
        # behavior unless the deployment explicitly opts this lane in.
        from ..config import Config

        runtime = Config().config
        search = runtime.get("search", {}) if isinstance(runtime, dict) else {}
        if not isinstance(search, dict):
            return False
        if str(search.get("provider") or "").strip().lower() != "local":
            return False
        if Features.is_enterprise() and search.get("task_semantic_enabled") is not True:
            return False
        if "task_semantic_enabled" in search:
            # Persisted settings are typed at the contract boundary.  Do not
            # let the string ``"false"`` become a truthy opt-in here.
            return search.get("task_semantic_enabled") is True
        return True
    except Exception:
        # A configuration failure must preserve the historical lexical tool,
        # while an explicitly enabled TaskIndexService fails closed below.
        return False


def task_semantic_search_enabled() -> bool:
    return _semantic_enabled_from_runtime()


def _string_uuid(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return str(value)


def _finite_unit_vector(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != DIMENSION:
        return False
    try:
        values = [float(item) for item in value]
    except (TypeError, ValueError, OverflowError):
        return False
    return all(math.isfinite(item) for item in values) and abs(
        sum(item * item for item in values) - 1
    ) <= 0.01


def _named_dense(point: Any) -> Any:
    vector = getattr(point, "vector", None)
    if isinstance(vector, Mapping):
        return vector.get("dense")
    return vector


class TaskIndexService:
    """Synchronize and query the private Task-only EG2 Qdrant collection."""

    def __init__(
        self,
        config: RagConfig | None = None,
        *,
        embedding: Any | None = None,
        client: Any | None = None,
        collection_name: str = TASK_INDEX_COLLECTION,
        enabled: bool | None = None,
        timeout_seconds: float = TASK_INDEX_TIMEOUT_SECONDS,
    ):
        self.config = config or get_rag_config()
        self.embedding = embedding or get_embedding(self.config.embedding)
        self.client = client
        self.collection_name = collection_name
        self.enabled = enabled
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("Task index timeout must be positive")
        self._initialized = False

    def is_enabled(self) -> bool:
        return (
            task_semantic_search_enabled()
            if self.enabled is None
            else self.enabled is True
        )

    async def _io(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        # The global lock protects other RAG services sharing QdrantLocal.
        if self.config.qdrant.local_path:
            return await run_index_io(method, *args, lock=_LOCAL_INDEX_IO_LOCK, **kwargs)
        return await run_index_io(method, *args, **kwargs)

    def _ensure_collection(self) -> None:
        if self.client is None:
            raise TaskIndexUnavailable("task_qdrant_unavailable")
        expected_suffix = "__eg2_" + EMBEDDING_FINGERPRINT[:16]
        if not self.collection_name.endswith(expected_suffix):
            raise TaskIndexError("task_index_identity_mismatch")
        if self.client.collection_exists(self.collection_name):
            params = self.client.get_collection(self.collection_name).config.params
            vectors = getattr(params, "vectors", None)
            if isinstance(vectors, dict):
                dense = vectors.get("dense")
            else:
                dense = None
            distance = getattr(dense, "distance", None) if dense is not None else None
            distance_value = getattr(distance, "value", distance)
            expected_distance = getattr(models.Distance.COSINE, "value", models.Distance.COSINE)
            if (
                dense is None
                or getattr(dense, "size", None) != DIMENSION
                or distance_value != expected_distance
            ):
                raise TaskIndexError("task_index_identity_mismatch")
            return
        self.client.create_collection(
            self.collection_name,
            vectors_config={
                "dense": models.VectorParams(size=DIMENSION, distance=models.Distance.COSINE)
            },
        )
        if not self.config.qdrant.local_path:
            for field in (
                "task_id",
                "project_id",
                "status",
                "assignee_ids",
                "embedding_fingerprint",
            ):
                try:
                    self.client.create_payload_index(
                        self.collection_name,
                        field,
                        models.PayloadSchemaType.KEYWORD,
                    )
                except Exception:
                    # Existing indexes are harmless; malformed collections were
                    # already rejected by the compatibility check above.
                    pass

    async def initialize(self) -> None:
        if not self.is_enabled():
            raise TaskIndexUnavailable("task_semantic_disabled")
        if self._initialized:
            return
        if not await self.embedding.initialize():
            raise TaskIndexUnavailable("task_embedding_unavailable")
        if self.client is None:
            qdrant = self.config.qdrant
            if qdrant.local_path:
                self.client = await self._io(SharedQdrantClient.get_client, qdrant.local_path)
            else:
                self.client = QdrantClient(
                    host=qdrant.host,
                    port=qdrant.port,
                    api_key=qdrant.api_key,
                )
        await self._io(self._ensure_collection)
        self._initialized = True

    @staticmethod
    def _snapshot(task: Any, assignee_ids: Iterable[Any] = ()) -> _TaskSnapshot:
        task_id = _string_uuid(getattr(task, "id", ""))
        project_id = _string_uuid(getattr(task, "project_id", ""))
        assignees = tuple(sorted({_string_uuid(value) for value in assignee_ids}))
        return _TaskSnapshot(
            id=task_id,
            project_id=project_id,
            title=str(getattr(task, "title", "") or ""),
            description=str(getattr(task, "description", "") or ""),
            status=str(getattr(task, "status", "") or ""),
            updated_at=getattr(task, "updated_at", None),
            deleted=getattr(task, "deleted_at", None) is not None,
            archived=getattr(task, "archived_at", None) is not None,
            assignee_ids=assignees,
        )

    @staticmethod
    def _payload(snapshot: _TaskSnapshot) -> dict[str, Any]:
        return {
            "task_id": snapshot.id,
            "project_id": snapshot.project_id,
            "status": snapshot.status,
            "assignee_ids": list(snapshot.assignee_ids),
            "content_hash": snapshot.content_hash,
            "updated_at": str(snapshot.updated_at or ""),
            "embedding_fingerprint": EMBEDDING_FINGERPRINT,
        }

    async def _load_page(
        self,
        session: Any,
        project_ids: Sequence[UUID],
        cursor: UUID | None,
    ) -> list[_TaskSnapshot]:
        if not project_ids:
            return []
        stmt = select(Task).where(Task.project_id.in_(list(project_ids)))
        if cursor is not None:
            stmt = stmt.where(Task.id > cursor)
        stmt = stmt.order_by(Task.id.asc()).limit(TASK_INDEX_PAGE_SIZE).execution_options(
            populate_existing=True
        )
        result = await session.execute(stmt)
        tasks = list(result.scalars().all())
        if not tasks:
            return []
        ids = [task.id for task in tasks]
        assignee_result = await session.execute(
            select(TaskAssignee.task_id, TaskAssignee.user_id).where(TaskAssignee.task_id.in_(ids))
        )
        assignees: dict[str, list[str]] = {}
        for task_id, user_id in assignee_result:
            assignees.setdefault(_string_uuid(task_id), []).append(_string_uuid(user_id))
        return [self._snapshot(task, assignees.get(_string_uuid(task.id), ())) for task in tasks]

    async def _fresh_rows(self, session: Any, ids: Sequence[str]) -> dict[str, Any]:
        if not ids:
            return {}
        try:
            parsed = [UUID(value) for value in ids]
        except (TypeError, ValueError) as exc:
            raise TaskIndexError("task_index_corrupt_point") from exc
        result = await session.execute(
            select(Task)
            .where(Task.id.in_(parsed))
            .execution_options(populate_existing=True)
        )
        return {_string_uuid(task.id): task for task in result.scalars().all()}

    async def _fresh_snapshots(
        self, session: Any, ids: Sequence[str]
    ) -> dict[str, _TaskSnapshot]:
        rows = await self._fresh_rows(session, ids)
        if not rows:
            return {}
        assignee_result = await session.execute(
            select(TaskAssignee.task_id, TaskAssignee.user_id).where(
                TaskAssignee.task_id.in_([UUID(value) for value in rows])
            )
        )
        assignees: dict[str, list[str]] = {}
        for task_id, user_id in assignee_result:
            assignees.setdefault(_string_uuid(task_id), []).append(_string_uuid(user_id))
        return {
            task_id: self._snapshot(task, assignees.get(task_id, ()))
            for task_id, task in rows.items()
        }

    async def _retrieve(self, ids: Sequence[str]) -> dict[str, Any]:
        if not ids:
            return {}
        rows = await self._io(
            self.client.retrieve,
            self.collection_name,
            list(ids),
            with_vectors=True,
            with_payload=True,
        )
        return {_string_uuid(getattr(row, "id", "")): row for row in rows}

    async def _embed(self, snapshots: Sequence[_TaskSnapshot]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(snapshots), TASK_EMBED_BATCH_SIZE):
            batch = snapshots[start : start + TASK_EMBED_BATCH_SIZE]
            try:
                result = await self.embedding.embed(
                    [item.description for item in batch],
                    titles=[item.title for item in batch],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise TaskIndexUnavailable("task_embedding_failed") from exc
            if len(result) != len(batch) or not all(_finite_unit_vector(vector) for vector in result):
                raise TaskIndexUnavailable("task_embedding_invalid_output")
            vectors.extend(result)
        return vectors

    async def _upsert_page(
        self,
        session: Any,
        snapshots: Sequence[_TaskSnapshot],
    ) -> tuple[set[str], set[str]]:
        if not snapshots:
            return set(), set()
        ids = [item.id for item in snapshots]
        existing = await self._retrieve(ids)
        to_embed: list[_TaskSnapshot] = []
        vectors: dict[str, list[float]] = {}
        needs_write: set[str] = set()
        points: list[Any] = []
        obsolete: set[str] = set()
        for item in snapshots:
            point = existing.get(item.id)
            if item.deleted or item.archived:
                obsolete.add(item.id)
                continue
            dense = _named_dense(point) if point is not None else None
            payload = getattr(point, "payload", None) or {} if point is not None else {}
            vector_invalid = not _finite_unit_vector(dense)
            content_changed = payload.get("content_hash") != item.content_hash
            identity_changed = payload.get("embedding_fingerprint") != EMBEDDING_FINGERPRINT
            metadata_changed = any(
                (
                    payload.get("project_id") != item.project_id,
                    payload.get("status") != item.status,
                    tuple(sorted(str(value) for value in (payload.get("assignee_ids") or [])))
                    != item.assignee_ids,
                    str(payload.get("updated_at") or "") != str(item.updated_at or ""),
                )
            )
            if vector_invalid or content_changed or identity_changed:
                to_embed.append(item)
                needs_write.add(item.id)
            else:
                vectors[item.id] = list(dense)
                if metadata_changed:
                    # Reuse the dense vector, but refresh current row metadata.
                    needs_write.add(item.id)
        if to_embed:
            embedded = await self._embed(to_embed)
            vectors.update({item.id: vector for item, vector in zip(to_embed, embedded)})

        # A task may change while the native worker is running.  Never publish
        # a vector whose input was not the current row at this boundary.
        fresh = await self._fresh_snapshots(session, ids)
        for item in snapshots:
            current = fresh.get(item.id)
            if current is None:
                if not item.deleted and not item.archived:
                    raise TaskIndexRace()
                continue
            if (
                current.project_id != item.project_id
                or current.content_hash != item.content_hash
                or current.deleted != item.deleted
                or current.archived != item.archived
                or current.status != item.status
                or current.assignee_ids != item.assignee_ids
            ):
                raise TaskIndexRace()

        for item in snapshots:
            if item.id in obsolete or item.id not in needs_write:
                continue
            vector = vectors.get(item.id)
            if not vector:
                raise TaskIndexUnavailable("task_vector_missing")
            points.append(
                models.PointStruct(
                    id=item.id,
                    vector={"dense": vector},
                    payload=self._payload(item),
                )
            )
        if points:
            await self._io(self.client.upsert, self.collection_name, points, wait=True)
        if obsolete:
            await self._io(
                self.client.delete,
                self.collection_name,
                models.PointIdsList(points=list(obsolete)),
                wait=True,
            )
        return {item.id for item in snapshots}, obsolete

    async def _withdraw_stale(
        self,
        session: Any,
        project_ids: Sequence[UUID],
        seen_ids: set[str],
        obsolete_ids: set[str],
    ) -> None:
        if not project_ids:
            return
        allowed = [str(value) for value in project_ids]
        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="project_id", match=models.MatchAny(any=allowed)
                ),
                models.FieldCondition(
                    key="embedding_fingerprint",
                    match=models.MatchValue(value=EMBEDDING_FINGERPRINT),
                ),
            ]
        )
        offset = None
        while True:
            rows, offset = await self._io(
                self.client.scroll,
                self.collection_name,
                scroll_filter=query_filter,
                limit=TASK_INDEX_PAGE_SIZE,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            if not rows:
                break
            candidate_ids = [
                _string_uuid(getattr(row, "id", ""))
                for row in rows
                if _string_uuid(getattr(row, "id", "")) not in seen_ids
                or _string_uuid(getattr(row, "id", "")) in obsolete_ids
            ]
            if candidate_ids:
                current = await self._fresh_rows(session, candidate_ids)
                withdraw: list[str] = []
                for row in rows:
                    task_id = _string_uuid(getattr(row, "id", ""))
                    if task_id not in candidate_ids:
                        continue
                    payload = getattr(row, "payload", None) or {}
                    point_project = str(payload.get("project_id") or "")
                    task = current.get(task_id)
                    if (
                        task is None
                        or task.deleted_at is not None
                        or task.archived_at is not None
                        or _string_uuid(getattr(task, "project_id", "")) != point_project
                    ):
                        withdraw.append(task_id)
                if withdraw:
                    await self._io(
                        self.client.delete,
                        self.collection_name,
                        models.PointIdsList(points=withdraw),
                        wait=True,
                    )
            if offset is None:
                break

    async def _sync_scope(
        self,
        session: Any,
        project_ids: Sequence[UUID],
        *,
        refresh_project_ids: Callable[[], Awaitable[Sequence[UUID]]] | None = None,
    ) -> tuple[UUID, ...]:
        cursor: UUID | None = None
        seen_ids: set[str] = set()
        obsolete_ids: set[str] = set()
        while True:
            page = await self._load_page(session, project_ids, cursor)
            if not page:
                break
            page_seen, page_obsolete = await self._upsert_page(session, page)
            seen_ids.update(page_seen)
            obsolete_ids.update(page_obsolete)
            cursor = UUID(page[-1].id)
            if len(page) < TASK_INDEX_PAGE_SIZE:
                break
        cleanup_projects = list(project_ids)
        if refresh_project_ids is not None:
            fresh_projects = {UUID(str(value)) for value in await refresh_project_ids()}
            cleanup_projects = [value for value in project_ids if value in fresh_projects]
        await self._withdraw_stale(session, cleanup_projects, seen_ids, obsolete_ids)
        return tuple(cleanup_projects)

    def _query_filter(
        self,
        project_ids: Sequence[UUID],
        *,
        status: str | None,
        assignee_id: UUID | None,
    ) -> Any:
        must: list[Any] = [
            models.FieldCondition(
                key="project_id",
                match=models.MatchAny(any=[str(value) for value in project_ids]),
            ),
            models.FieldCondition(
                key="embedding_fingerprint",
                match=models.MatchValue(value=EMBEDDING_FINGERPRINT),
            ),
        ]
        if status:
            must.append(models.FieldCondition(key="status", match=models.MatchValue(value=status)))
        if assignee_id is not None:
            must.append(
                models.FieldCondition(
                    key="assignee_ids",
                    match=models.MatchAny(any=[str(assignee_id)]),
                )
            )
        return models.Filter(must=must)

    async def _search_once(
        self,
        session: Any,
        project_ids: Sequence[UUID],
        *,
        search: str,
        status: str | None,
        assignee_id: UUID | None,
        limit: int,
        refresh_project_ids: Callable[[], Awaitable[Sequence[UUID]]] | None,
    ) -> list[TaskSearchHit]:
        if refresh_project_ids is not None:
            initial_projects = tuple(UUID(str(value)) for value in await refresh_project_ids())
        else:
            initial_projects = tuple(project_ids)
        if not initial_projects:
            return []
        effective_projects = await self._sync_scope(
            session,
            initial_projects,
            refresh_project_ids=refresh_project_ids,
        )
        if refresh_project_ids is not None:
            final_projects = tuple(UUID(str(value)) for value in await refresh_project_ids())
            if set(final_projects) - set(initial_projects):
                # A newly granted Project was not part of this pass's corpus.
                # Retry from the new scope so the first result cannot be partial.
                raise TaskIndexRace()
            effective_projects = tuple(
                value for value in effective_projects if value in set(final_projects)
            )
        if not effective_projects:
            return []
        query = await self.embedding.embed_query(search)
        if not _finite_unit_vector(query):
            raise TaskIndexUnavailable("task_query_embedding_failed")
        response = await self._io(
            self.client.query_points,
            collection_name=self.collection_name,
            query=query,
            using="dense",
            query_filter=self._query_filter(
                effective_projects, status=status, assignee_id=assignee_id
            ),
            with_payload=True,
            limit=min(TASK_SEARCH_MAX_POINTS, max(limit * TASK_SEARCH_OVERFETCH, limit)),
        )
        points = list(getattr(response, "points", ()) or ())
        if not points:
            return []
        ids = []
        malformed_point = False
        point_scores: dict[int, float] = {}
        for point in points:
            task_id = _string_uuid(getattr(point, "id", ""))
            try:
                UUID(task_id)
            except (TypeError, ValueError):
                malformed_point = True
                continue
            ids.append(task_id)
            try:
                score = float(getattr(point, "score"))
            except (AttributeError, TypeError, ValueError, OverflowError):
                malformed_point = True
                continue
            if not math.isfinite(score):
                malformed_point = True
                continue
            point_scores[id(point)] = score
        if not ids:
            if malformed_point:
                raise TaskIndexError("task_index_corrupt_point")
            return []
        current = await self._fresh_snapshots(session, ids)
        if refresh_project_ids is not None:
            post_query_projects = {
                UUID(str(value)) for value in await refresh_project_ids()
            }
            if post_query_projects != set(effective_projects):
                raise TaskIndexRace()
        allowed_projects = {str(value) for value in effective_projects}
        ordered: list[TaskSearchHit] = []
        stale = False
        for point in points:
            task_id = _string_uuid(getattr(point, "id", ""))
            if task_id not in ids:
                # A malformed point cannot be hydrated, but a valid sibling
                # remains safe to return.  An all-malformed page was rejected
                # above with task_index_corrupt_point.
                continue
            score = point_scores.get(id(point))
            if score is None:
                # A malformed score is a corrupt point.  Keep valid siblings,
                # but never turn an invalid backend score into a normal hit.
                continue
            payload = getattr(point, "payload", None) or {}
            snapshot = current.get(task_id)
            if snapshot is None:
                stale = True
                continue
            if (
                snapshot.deleted
                or snapshot.archived
                or snapshot.project_id not in allowed_projects
                or (status and snapshot.status != status)
                or (assignee_id is not None and str(assignee_id) not in snapshot.assignee_ids)
                or payload.get("content_hash") != snapshot.content_hash
                or payload.get("project_id") != snapshot.project_id
                or payload.get("embedding_fingerprint") != EMBEDDING_FINGERPRINT
            ):
                stale = True
                continue
            if not any(hit.task_id == task_id for hit in ordered):
                ordered.append(
                    TaskSearchHit(
                        task_id=task_id,
                        content_hash=snapshot.content_hash,
                        score=score,
                    )
                )
            if len(ordered) >= limit:
                break
        if malformed_point and not ordered:
            raise TaskIndexError("task_index_corrupt_point")
        if stale:
            raise TaskIndexRace()
        return ordered

    async def search_task_hits(
        self,
        session: Any,
        project_ids: Sequence[UUID],
        *,
        search: str,
        status: str | None = None,
        assignee_id: UUID | None = None,
        limit: int = 25,
        refresh_project_ids: Callable[[], Awaitable[Sequence[UUID]]] | None = None,
    ) -> list[TaskSearchHit]:
        if not self.is_enabled():
            raise TaskIndexUnavailable("task_semantic_disabled")
        if not search.strip():
            return []
        if not project_ids:
            return []
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self.initialize()
                for attempt in range(TASK_INDEX_RACE_RETRIES + 1):
                    try:
                        return await self._search_once(
                            session,
                            project_ids,
                            search=search,
                            status=status,
                            assignee_id=assignee_id,
                            limit=max(1, min(int(limit or 25), 50)),
                            refresh_project_ids=refresh_project_ids,
                        )
                    except TaskIndexRace:
                        if attempt >= TASK_INDEX_RACE_RETRIES:
                            raise
        except asyncio.TimeoutError as exc:
            raise TaskIndexUnavailable("task_index_timeout") from exc
        except asyncio.CancelledError:
            raise
        except TaskIndexError:
            raise
        except Exception as exc:
            raise TaskIndexUnavailable("task_index_failed") from exc


_task_index: TaskIndexService | None = None


def get_task_index_service() -> TaskIndexService:
    global _task_index
    if _task_index is None:
        _task_index = TaskIndexService()
    return _task_index


def reset_task_index_service() -> None:
    global _task_index
    _task_index = None
