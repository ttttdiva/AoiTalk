"""Resumable, ACL-filtered multimodal hybrid index with original retrieval."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import uuid

from filelock import FileLock, Timeout as FileLockTimeout
from qdrant_client import QdrantClient, models

from .config import RagConfig, get_rag_config
from .embedding import get_embedding
from .embedding_identity import (
    DIMENSION,
    EMBEDDING_FINGERPRINT,
    embedding_collection_name,
)
from .media import MediaPreparationService, MediaPreparationError, PreparedMediaChunk
from .qdrant_client import SharedQdrantClient
from .index_io import run_index_io
from ..knowledge.index_service import HashingSparseEncoder, _LOCAL_INDEX_IO_LOCK
from ..security.field_crypto import encrypt_text, decrypt_text_if_needed
from ..services.search_sources import SourceRef, SourceUnavailable
from ..services.media_index_maintenance import (
    prepare_lifecycle_schema, record_cleanup_verified, source_file_lock_path,
)


MAX_READY_SOURCES = 100_000
MAX_SEARCH_RESULTS = 100
SEARCH_OVERFETCH = 4

# ``SourceUnavailable`` is also used for an actor losing a read grant.  Only
# reasons which describe the canonical source itself being gone/retired may
# withdraw a shared derivative.  A generic ``source_unavailable`` is kept
# actor-scoped so one reader cannot delete another reader's ready source.
GLOBAL_SOURCE_UNAVAILABLE_REASONS = frozenset({
    "canonical_source_deleted",
    "generation_output_not_registered_as_managed_asset",
    "meeting_audio_discarded_after_success",
    "original_missing",
    "original_path_invalid",
    "privacy_masking_source",
    "remote_growi_has_no_local_media_original",
    "source_deleted",
    "source_globally_unavailable",
    "workspace_file_not_indexable",
})
# Workspace Files text units decode UTF-8 first, then Japanese Windows CP932.
WORKSPACE_TEXT_CHARS = 4000
WORKSPACE_OFFICE_CHARS = 4000
RETRYABLE_SOURCE_UNAVAILABLE_REASONS = frozenset({
    "original_storage_failure",
})


class _LocalSourceValues(list[str]):
    """Request-owned MatchAny list with constant-time local membership."""

    def __init__(self, values):
        super().__init__(values)
        self._members = frozenset(self)

    def __contains__(self, value):
        return isinstance(value, str) and value in self._members


OLE_COMPOUND_HEADER = bytes.fromhex("D0CF11E0A1B11AE1")


class CanonicalMediaExclusion(MediaPreparationError):
    """Formal exclusion from operator all-reader reconciliation only.

    Ordinary actor resolution uses SourceUnavailable and cannot withdraw
    another reader's READY state.

    ``content_hash`` is the hash of the bytes actually inspected; sync_source
    publishes the exclusion only when it equals the verified inventory hash.
    """

    def __init__(self, reason, message, content_hash=None):
        super().__init__(reason, message)
        self.content_hash = content_hash


class MigrationLedger:
    """Durable per-source checkpoints; vectors are verified before ready state."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS embedding_sources (
                source_key TEXT PRIMARY KEY, kind TEXT NOT NULL, source_id TEXT NOT NULL,
                identity TEXT NOT NULL, status TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
                total INTEGER, error TEXT, verified_hash TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("""CREATE TABLE IF NOT EXISTS embedding_attempts (
                actor TEXT, source_key TEXT, identity TEXT, status TEXT, error TEXT,
                PRIMARY KEY(actor,source_key,identity))""")
            prepare_lifecycle_schema(db)

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def mark(
        self, ref, status, *, completed=0, total=None, error=None, verified_hash=None
    ):
        with self.connect() as db:
            db.execute(
                """INSERT INTO embedding_sources
                (source_key,kind,source_id,identity,status,completed,total,error,verified_hash)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET
                identity=excluded.identity,status=excluded.status,completed=excluded.completed,
                total=excluded.total,error=excluded.error,verified_hash=excluded.verified_hash,
                updated_at=CURRENT_TIMESTAMP""",
                (
                    ref.key,
                    ref.kind,
                    ref.id,
                    EMBEDDING_FINGERPRINT,
                    status,
                    completed,
                    total,
                    error,
                    verified_hash,
                ),
            )

    def ready(self):
        with self.connect() as db:
            refs = [
                SourceRef(kind, source_id)
                for kind, source_id in db.execute(
                    "SELECT kind,source_id FROM embedding_sources WHERE identity=? AND status='ready' LIMIT ?",
                    (EMBEDDING_FINGERPRINT, MAX_READY_SOURCES + 1),
                )
            ]
        if len(refs) > MAX_READY_SOURCES:
            raise RuntimeError("Media source scope exceeds 100000; partition the search inventory")
        return refs

    def state(self, ref):
        with self.connect() as db:
            row = db.execute(
                "SELECT status FROM embedding_sources WHERE source_key=? AND identity=?",
                (ref.key, EMBEDDING_FINGERPRINT),
            ).fetchone()
        return row[0] if row else None

    def is_ready(self, ref):
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM embedding_sources WHERE source_key=? AND identity=? AND status='ready'",
                (ref.key, EMBEDDING_FINGERPRINT),
            ).fetchone() is not None

    def attempt(self, ref, actor, status, error):
        with self.connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO embedding_attempts
                (actor,source_key,identity,status,error) VALUES(?,?,?,?,?)""",
                (str(actor), ref.key, EMBEDDING_FINGERPRINT, status, error),
            )

    def progress(self, *, actor=None):
        with self.connect() as db:
            rows = db.execute(
                "SELECT status,count(*),sum(completed),sum(total) FROM embedding_sources WHERE identity=? GROUP BY status",
                (EMBEDDING_FINGERPRINT,),
            ).fetchall()
            failures = db.execute(
                "SELECT source_key,status,error FROM embedding_sources WHERE identity=? AND status IN ('failed','excluded')",
                (EMBEDDING_FINGERPRINT,),
            ).fetchall()
            attempts = db.execute(
                "SELECT source_key,status,error FROM embedding_attempts WHERE identity=?"
                + (" AND actor=?" if actor is not None else ""),
                (EMBEDDING_FINGERPRINT, str(actor))
                if actor is not None
                else (EMBEDDING_FINGERPRINT,),
            ).fetchall()
        return {
            "identity": EMBEDDING_FINGERPRINT,
            "states": {
                s: {"sources": c, "completed": done or 0, "total": total}
                for s, c, done, total in rows
            },
            "failures": [
                {"source": key, "status": s, "reason": reason}
                for key, s, reason in failures
            ],
            "attempts": [
                {"source": key, "status": s, "reason": reason}
                for key, s, reason in attempts
            ],
        }


class MediaIndexService:
    def __init__(
        self,
        config: RagConfig | None = None,
        *,
        ledger_path="data/search/embeddinggemma2.sqlite",
        client=None,
    ):
        self.config = config or get_rag_config()
        self.collection_name = embedding_collection_name("aoitalk_media")
        self.embedding = get_embedding(self.config.embedding)
        self.sparse = HashingSparseEncoder()
        self.ledger = MigrationLedger(ledger_path)
        self.preparation = MediaPreparationService()
        self.client = client
        self._initialized = False
        self._source_locks: dict[str, threading.Lock] = {}
        self._lock_guard = threading.Lock()

    async def _io(self, method, *args, **kwargs):
        return await run_index_io(
            method, *args,
            lock=_LOCAL_INDEX_IO_LOCK if self.config.qdrant.local_path else None,
            **kwargs,
        )

    @staticmethod
    async def _next_chunk(iterator):
        # Rendering must finish before close, even during all-Task teardown.
        return await run_index_io(next, iterator, None)

    async def _source_is_globally_unavailable(self, resolver, ref, reason):
        """Distinguish canonical removal from an actor-only ACL rejection.

        ``SearchSourceResolver`` deliberately hides ACL details behind one
        non-disclosing ``SourceUnavailable`` exception.  A failed actor
        resolution therefore cannot by itself justify deleting a shared
        point.  We inspect only registered structural rows (never original
        bytes or paths) and fail closed when that probe is unavailable.
        """
        if reason in GLOBAL_SOURCE_UNAVAILABLE_REASONS:
            return True

        session = getattr(resolver, "session", None)
        if session is None:
            return False
        try:
            from ..memory.models import (
                ConversationMessage,
                ConversationSession,
                DocsLibrary,
                GeneratedMedia,
                GenerationOutput,
                KnowledgeAttachment,
                KnowledgeDocument,
                KnowledgeSource,
                KnowledgeNode,
                MeetingProcessingJob,
                Project,
                Task,
                TaskAttachment,
            )

            async def get(model, identifier):
                return await session.get(model, identifier, populate_existing=True)

            raw_id = ref.id.split(":", 1)[0] if ref.kind in {
                "chat_attachment", "conversation_message"
            } else ref.id
            identifier = uuid.UUID(str(raw_id))

            if ref.kind == "task_attachment":
                attachment = await get(TaskAttachment, identifier)
                if attachment is None:
                    return True
                task = await get(Task, attachment.task_id)
                if (
                    task is None
                    or task.deleted_at is not None
                    or task.project_id != attachment.project_id
                ):
                    return True
                project = await get(Project, attachment.project_id)
                return project is None or project.deleted_at is not None

            if ref.kind in {"chat_attachment", "conversation_message"}:
                message = await get(ConversationMessage, identifier)
                if (
                    message is None
                    or message.deleted_at is not None
                    or message.is_active_branch is False
                ):
                    return True
                conversation = await get(ConversationSession, message.session_id)
                if conversation is None or conversation.deleted_at is not None:
                    return True
                if ref.kind == "chat_attachment":
                    try:
                        index = int(ref.id.split(":", 1)[1])
                        attachments = (message.message_metadata or {}).get("attachments") or []
                        item = attachments[index]
                        return not isinstance(item, dict) or not str(item.get("path") or "").strip()
                    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                        return True
                return False

            if ref.kind == "generated_media":
                media = await get(GeneratedMedia, identifier)
                if media is None:
                    return True
                # Pending/unknown states may still receive their durable
                # completion notification.  Only canonical terminal failure
                # states prove that no original will become available.
                if media.status in {"failed", "cancelled"}:
                    return True
                if media.status != "succeeded":
                    return False
                if media.context_type == "conversation":
                    conversation = await get(ConversationSession, uuid.UUID(str(media.context_id)))
                    if conversation is None or conversation.deleted_at is not None:
                        return True
                    if conversation.project_id:
                        project = await get(Project, uuid.UUID(str(conversation.project_id)))
                        return project is None or project.deleted_at is not None
                return False

            if ref.kind == "generation_output":
                output = await get(GenerationOutput, identifier)
                if output is None:
                    return True
                if getattr(output, "project_id", None):
                    project = await get(Project, uuid.UUID(str(output.project_id)))
                    return project is None or project.deleted_at is not None
                return False

            if ref.kind == "docs_attachment":
                attachment = await get(KnowledgeAttachment, identifier)
                if attachment is None:
                    return True
                node = await get(KnowledgeNode, attachment.node_id)
                if node is None or node.archived_at is not None:
                    return True
                if node.project_id:
                    project = await get(Project, uuid.UUID(str(node.project_id)))
                    if project is None or project.deleted_at is not None:
                        return True
                library = await get(DocsLibrary, node.docs_library_id)
                return library is None

            if ref.kind == "knowledge_document":
                document = await get(KnowledgeDocument, identifier)
                if document is None or document.status != "active":
                    return True
                source = await get(KnowledgeSource, document.source_id)
                return source is None

            if ref.kind == "workspace_file":
                from ..memory.models import WorkspaceFileEntry

                entry = await get(WorkspaceFileEntry, identifier)
                if entry is None or entry.deleted_at is not None or entry.status != "indexable":
                    return True
                if entry.scope == "project":
                    project = await get(Project, entry.owner_id)
                    return project is None or project.deleted_at is not None
                return False

            if ref.kind == "meeting_audio":
                job = await get(MeetingProcessingJob, identifier)
                if job is None or job.status == "succeeded":
                    return True
                request = job.request_json or {}
                project_id = request.get("project_id") if isinstance(request, dict) else None
                if project_id:
                    project = await get(Project, uuid.UUID(str(project_id)))
                    return project is None or project.deleted_at is not None
                return False
        except (ValueError, TypeError, AttributeError, KeyError):
            return False
        except Exception:
            # Database/storage failures are not proof of canonical deletion.
            return False
        return False

    async def initialize(self, *, require_embedding=True):
        if require_embedding and not await self.embedding.initialize():
            return False
        if self._initialized:
            return True
        if self.client is None:
            cfg = self.config.qdrant
            self.client = (
                (await self._io(SharedQdrantClient.get_client, cfg.local_path))
                if cfg.local_path
                else QdrantClient(host=cfg.host, port=cfg.port, api_key=cfg.api_key)
            )
        await self._io(self._ensure_collection)
        self._initialized = True
        return True

    def _ensure_collection(self, *, allow_create=True):
        if self.client.collection_exists(self.collection_name):
            params = self.client.get_collection(self.collection_name).config.params
            dense = (
                params.vectors.get("dense")
                if isinstance(params.vectors, dict)
                else None
            )
            if (
                dense is None
                or dense.size != DIMENSION
                or "sparse" not in (params.sparse_vectors or {})
            ):
                raise RuntimeError(
                    "Embedding identity mismatch; explicit reindex required"
                )
            return
        if not allow_create:
            raise RuntimeError("Verified media collection is unavailable; adoption stopped")
        self.client.create_collection(
            self.collection_name,
            vectors_config={
                "dense": models.VectorParams(
                    size=DIMENSION, distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config={"sparse": models.SparseVectorParams()},
        )
        if not self.config.qdrant.local_path:
            for name in ["source_key", "embedding_fingerprint"]:
                self.client.create_payload_index(
                    self.collection_name, name, models.PayloadSchemaType.KEYWORD
                )

    @staticmethod
    def _hash(path):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _source_file_lock_path(self, ref):
        # Shared with the maintenance purge so it serializes with this writer.
        return source_file_lock_path(self.ledger.path, ref.key)

    @staticmethod
    def _decode_workspace_text(raw: bytes) -> str:
        for encoding in ("utf-8-sig", "cp932"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _workspace_text_chunks(self, source, text, content_hash, kind):
        """Deterministic text units for Files (re-run by read_original)."""
        size = WORKSPACE_TEXT_CHARS if kind == "text" else WORKSPACE_OFFICE_CHARS
        for offset in range(0, len(text), size):
            unit = text[offset:offset + size]
            point = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source.ref.key}:{kind}:{offset}"))
            yield PreparedMediaChunk(point, "text", {}, unit, {
                **source.metadata, "file_name": source.title, "content_hash": content_hash,
                "text_offset": offset, "text_start": offset, "text_end": offset + len(unit),
                "text_length": len(text),
            })

    def _workspace_streamed_chunks(self, source, content_hash):
        """Fixed-size units over the streamed cell text (offsets stay deterministic).

        The workbook is parsed by an isolated child process that re-verifies the
        original's hash, so a native parser crash cannot take this process down.
        """
        from ..tools.documents.office_reader import OfficeSecurityError
        from ..tools.documents.xlsx_stream_isolated import (
            OfficeSourceChangedError, OfficeWorkerCrashError, iter_xlsx_text_isolated,
        )

        buffer, offset = "", 0

        def unit(text):
            point = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source.ref.key}:xlsx_stream:{offset}"))
            return PreparedMediaChunk(point, "text", {}, text, {
                **source.metadata, "file_name": source.title, "content_hash": content_hash,
                "text_offset": offset, "text_start": offset, "text_end": offset + len(text),
            })

        try:
            with contextlib.closing(iter_xlsx_text_isolated(source.path, content_hash)) as pieces:
                for piece in pieces:
                    buffer += piece
                    while len(buffer) >= WORKSPACE_OFFICE_CHARS:
                        yield unit(buffer[:WORKSPACE_OFFICE_CHARS])
                        buffer, offset = buffer[WORKSPACE_OFFICE_CHARS:], offset + WORKSPACE_OFFICE_CHARS
        except OfficeSourceChangedError as exc:
            raise RuntimeError("Original changed during indexing; retry required") from exc
        except OfficeWorkerCrashError as exc:
            raise MediaPreparationError("office_worker_crashed", str(exc)) from exc
        except OfficeSecurityError as exc:
            raise MediaPreparationError("office_conversion_failed", str(exc)) from exc
        if buffer.strip():
            yield unit(buffer)
        elif offset == 0:
            raise CanonicalMediaExclusion("empty_text", "本文がありません", content_hash)

    def _workspace_chunks(self, source):
        fmt = source.metadata.get("index_format")
        if fmt in {"text", "office"}:
            content_hash = self._hash(source.path)
            raw = source.path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != content_hash:
                raise RuntimeError("Original changed during indexing; retry required")
            if fmt == "text":
                text = self._decode_workspace_text(raw)
            else:
                from ..tools.documents.office_reader import convert_office_bytes_to_markdown

                if raw[:8] == OLE_COMPOUND_HEADER:
                    # An OOXML-suffixed OLE container is an IRM/password
                    # protected package; it cannot be read or repaired here.
                    raise CanonicalMediaExclusion("office_encrypted_container", "暗号化・権限保護されたOfficeファイルです", content_hash)
                is_xlsx = source.path.suffix.lower() == ".xlsx"
                if is_xlsx and raw[:2] == b"PK":
                    from ..tools.documents.xlsx_stream import should_stream_xlsx

                    if should_stream_xlsx(raw):
                        # Too large to buffer: read it row by row (route depends on the file only).
                        yield from self._workspace_streamed_chunks(source, content_hash)
                        return
                converted = convert_office_bytes_to_markdown(raw, source.path.name)
                if not converted.get("success"):
                    from ..tools.documents.office_reader import OFFICE_OUTPUT_LIMIT_ERROR, OFFICE_TIMEOUT_ERROR

                    error = str(converted.get("error") or "Office変換に失敗しました")
                    if is_xlsx and error in {OFFICE_OUTPUT_LIMIT_ERROR, OFFICE_TIMEOUT_ERROR}:
                        # Too large/slow to buffer. Streamed units use their own id namespace
                        # and read_original checks lexical_hash, so a route that differs
                        # between runs yields "unavailable", never another run's text.
                        yield from self._workspace_streamed_chunks(source, content_hash)
                        return
                    raise MediaPreparationError("office_conversion_failed", error)
                text = str(converted.get("content") or "")
            if not text.strip():
                raise CanonicalMediaExclusion("empty_text", "本文がありません", content_hash)
            yield from self._workspace_text_chunks(source, text, content_hash, fmt)
            return
        if fmt in {"audio", "video"}:
            from ..services.workspace_file_index import load_files_index_settings, owner_rules

            settings = load_files_index_settings()
            limit = owner_rules(settings, source.metadata.get("files_scope") or "user",
                                source.metadata.get("files_owner_id") or "").max_duration_seconds
            probe = self.preparation._probe(source.path)
            streams = [stream for stream in probe.get("streams") or []
                       if stream.get("codec_type") in {"audio", "video"}]
            durations = []
            for stream in streams:
                try:
                    durations.append(self.preparation._stream_duration(probe, stream))
                except MediaPreparationError:
                    continue
            if durations and max(durations) > limit:
                raise CanonicalMediaExclusion("media_duration_limit", "メディアが索引対象の長さ上限を超えています")
        yield from self.preparation.iter_prepare(
            source.path,
            source_id=source.ref.key,
            source_kind=source.ref.kind,
            metadata={**source.metadata, "file_name": source.title},
            lexical_text=source.text,
            lexical_segments=source.lexical_segments,
            mime_type=source.metadata.get("mime_type"),
        )

    def _chunks(self, source):
        if source.ref.kind == "workspace_file":
            yield from self._workspace_chunks(source)
            return
        text_suffixes = {
            ".md",
            ".txt",
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".json",
            ".yaml",
            ".yml",
            ".html",
            ".css",
            ".sh",
            ".ps1",
            ".sql",
            ".rs",
            ".go",
            ".java",
            ".c",
            ".cpp",
            ".h",
            ".cs",
            ".toml",
            ".xml",
            ".csv",
            ".log",
        }
        if (
            source.path.suffix.lower() in text_suffixes
            or Path(source.title).suffix.lower() in text_suffixes
            or str(source.metadata.get("mime_type") or "").startswith("text/")
        ):
            # Text/code attachments share exactly the same document encoding.
            content_hash = self._hash(source.path)
            with source.path.open("r", encoding="utf-8", errors="replace") as handle:
                offset = 0
                while text := handle.read(4000):
                    point = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL, f"{source.ref.key}:text:{offset}"
                        )
                    )
                    yield PreparedMediaChunk(
                        point,
                        "text",
                        {},
                        text,
                        {
                            **source.metadata,
                            "file_name": source.title,
                            "content_hash": content_hash,
                            "text_offset": offset,
                        },
                    )
                    offset += len(text)
        else:
            yield from self.preparation.iter_prepare(
                source.path,
                source_id=source.ref.key,
                source_kind=source.ref.kind,
                metadata={**source.metadata, "file_name": source.title},
                lexical_text=source.text,
                lexical_segments=source.lexical_segments,
                mime_type=source.metadata.get("mime_type"),
            )

    async def _sync_chunk_batch(
        self, source, chunks, content_hash, expected_lexical_hashes, *, reuse_only
    ):
        """Reuse valid points and persist one bounded text batch or media unit."""
        ref = source.ref
        records = await self._io(
            self.client.retrieve, self.collection_name,
            [chunk.id for chunk in chunks], with_vectors=True,
        )
        existing = {str(point.id): point for point in records}
        changed = []
        for chunk in chunks:
            point = existing.get(chunk.id)
            payload = (point.payload or {}) if point else {}
            dense = (point.vector or {}).get("dense", []) if point else []
            valid = (
                len(dense) == DIMENSION
                and all(math.isfinite(x) for x in dense)
                and abs(sum(x * x for x in dense) - 1) <= 0.01
            )
            # A Files move/rename changes only its descriptive payload.
            lexical_hash = hashlib.sha256(
                (chunk.lexical_text + ("" if ref.kind == "workspace_file" else source.title)).encode()
            ).hexdigest()
            expected_lexical_hashes[chunk.id] = lexical_hash
            if (
                not valid
                or payload.get("content_hash") != content_hash
                or payload.get("lexical_hash") != lexical_hash
                or payload.get("embedding_fingerprint") != EMBEDDING_FINGERPRINT
                or payload.get("source_key") != ref.key
            ):
                if reuse_only:
                    raise RuntimeError("Verified media unit differs from current original; adoption stopped")
                changed.append(chunk)
            elif (ref.kind == "workspace_file" and not reuse_only
                  and (payload.get("file_name") != source.title
                       or payload.get("files_location") != source.metadata.get("files_location"))):
                await self._io(
                    self.client.set_payload, self.collection_name,
                    {"file_name": source.title, "files_location": source.metadata.get("files_location")},
                    points=[chunk.id], wait=True,
                )
        if not changed:
            return
        vectors = (
            await self.embedding.embed(
                [chunk.lexical_text for chunk in changed],
                titles=[source.title] * len(changed),
            )
            if changed[0].modality == "text"
            else await self.embedding.encode_media([changed[0].model_input])
        )
        # Reject the whole response before writing or checkpointing any of it.
        if len(vectors) != len(changed) or any(
            len(vector) != DIMENSION
            or not all(math.isfinite(value) for value in vector)
            or abs(sum(value * value for value in vector) - 1) > 0.01
            for vector in vectors
        ):
            raise RuntimeError(self.embedding.last_error or "Media embedding failed")
        points = []
        for chunk, vector in zip(changed, vectors):
            points.append(models.PointStruct(
                id=chunk.id,
                vector={
                    "dense": vector,
                    "sparse": self.sparse.encode(source.title + " " + chunk.lexical_text),
                },
                payload={
                    **chunk.metadata,
                    "source_key": ref.key,
                    "source_kind": ref.kind,
                    "original_id": ref.id,
                    "modality": chunk.modality,
                    "content_hash": content_hash,
                    "lexical_hash": expected_lexical_hashes[chunk.id],
                    "embedding_fingerprint": EMBEDDING_FINGERPRINT,
                    "lexical_text": encrypt_text(chunk.lexical_text, aad=f"media-index:{chunk.id}"),
                },
            ))
        await self._io(self.client.upsert, self.collection_name, points, wait=True)

    async def sync_source(self, resolver, ref, *, reuse_only=False):
        with self._lock_guard:
            lock = self._source_locks.setdefault(ref.key, threading.Lock())
        # Thread lock permits shared service use across tool-call event loops.
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
        # The acquire/release calls run in executor threads that may differ;
        # disable FileLock's thread-local recursion state so release always
        # closes the handle acquired by the worker thread.
        file_lock = FileLock(
            str(self._source_file_lock_path(ref)), thread_local=False
        )
        try:
            # FileLock acquisition runs in the executor so another process's
            # wait never blocks this event loop.  run_index_io also drains a
            # cancellation until the bounded timeout has released ownership.
            await run_index_io(file_lock.acquire, timeout=30)
        except FileLockTimeout:
            lock.release()
            reason = "media_index_lock_timeout"
            self.ledger.attempt(ref, resolver.scope.user_id, "failed", reason)
            self.ledger.mark(ref, "failed", error=reason)
            return {"status": "failed", "reason": reason}
        except asyncio.CancelledError:
            try:
                if file_lock.is_locked:
                    await run_index_io(file_lock.release)
            finally:
                lock.release()
            raise
        except BaseException:
            lock.release()
            raise
        completed = 0
        iterator = None
        before_hash = None
        try:
            if ref.kind in {"chat_attachment", "conversation_message"} and await resolver.privacy_masked(ref):
                if reuse_only:
                    raise RuntimeError("Privacy state changed after verification; adoption stopped")
                return await self._withdraw_private_source(ref)
            source = await resolver.resolve(ref)
            before_hash = await run_index_io(self._hash, source.path)
            if ref.kind == "workspace_file" and before_hash != source.metadata.get("content_hash"):
                # The inventory row must describe these bytes; the scanner
                # re-hashes and re-queues a changed file.
                raise RuntimeError("Original changed since the Files scan; rescan required")
            self.ledger.mark(ref, "running")
            if reuse_only:
                if self.client is None:
                    raise RuntimeError("Verified media collection is unavailable; adoption stopped")
                await self._io(self._ensure_collection, allow_create=False)
            elif not await self.initialize():
                raise RuntimeError(
                    self.embedding.last_error or "EmbeddingGemma 2 unavailable"
                )
            retained = []
            expected_lexical_hashes = {}
            iterator = self._chunks(source)
            batch_size = max(1, min(
                int(self.config.indexing.batch_size), int(self.config.embedding.batch_size), 32
            ))
            pending = []

            async def flush():
                nonlocal completed
                if not pending:
                    return
                await self._sync_chunk_batch(
                    source, pending, before_hash, expected_lexical_hashes,
                    reuse_only=reuse_only,
                )
                # Checkpoint only acknowledged writes. On failure, deterministic
                # ids and hash validation also recover a partially written batch.
                completed += len(pending)
                self.ledger.mark(ref, "running", completed=completed)
                pending.clear()

            # Text is bounded by the encoder's request limit. Decoded media
            # still advances one page/30s/32frames at a time.
            while True:
                chunk = await self._next_chunk(iterator)
                if chunk is None or chunk.modality != "text":
                    await flush()
                if chunk is None:
                    break
                retained.append(chunk.id)
                pending.append(chunk)
                if chunk.modality != "text" or len(pending) >= batch_size:
                    await flush()
            if not completed:
                raise RuntimeError("Original has no indexable units")
            # Validate DB ACL and original integrity again before publishing ready.
            current = await resolver.resolve(ref)
            if await run_index_io(self._hash, current.path) != before_hash:
                raise RuntimeError("Original changed during indexing; retry required")
            for offset in range(0, len(retained), 64):
                batch_ids = retained[offset : offset + 64]
                records = await self._io(
                    self.client.retrieve,
                    self.collection_name,
                    batch_ids,
                    with_vectors=True,
                )
                if {str(point.id) for point in records} != set(batch_ids):
                    raise RuntimeError("Index verification failed; migration not ready")
                for point in records:
                    payload = point.payload or {}
                    dense = (point.vector or {}).get("dense", [])
                    if (
                        len(dense) != DIMENSION
                        or not all(math.isfinite(value) for value in dense)
                        or abs(sum(value * value for value in dense) - 1) > 0.01
                        or payload.get("embedding_fingerprint") != EMBEDDING_FINGERPRINT
                        or payload.get("content_hash") != before_hash
                        or payload.get("lexical_hash") != expected_lexical_hashes[str(point.id)]
                        or payload.get("source_key") != ref.key
                    ):
                        raise RuntimeError("Index verification failed; migration not ready")
            # Reuse-only adoption never repairs or deletes the verified target.
            selector = models.Filter(
                must=[models.FieldCondition(key="source_key", match=models.MatchValue(value=ref.key))],
                must_not=[models.HasIdCondition(has_id=retained)],
            )
            if reuse_only:
                stale = await self._io(self.client.count, self.collection_name,
                                      count_filter=selector, exact=True)
                if stale.count:
                    raise RuntimeError("Verified media source has obsolete units; adoption stopped")
            else:
                await self._io(self.client.delete, self.collection_name,
                               models.FilterSelector(filter=selector), wait=True)
            self.ledger.mark(
                ref,
                "ready",
                completed=completed,
                total=completed,
                verified_hash=before_hash,
            )
            return {"status": "ready", "units": completed}
        except asyncio.CancelledError:
            self.ledger.mark(
                ref,
                "interrupted",
                completed=completed,
                error="cancelled; rerun resumes verified units",
            )
            raise
        except CanonicalMediaExclusion as exc:
            # The exclusion is terminal only for the bytes that were inspected:
            # they must match the verified inventory hash and the file must
            # still hash the same now. Otherwise retry, never publish an old hash.
            # Exclusions raised by the resolver before any original exists
            # (before_hash is None) are canonical and need no byte binding.
            stable = True
            if before_hash is not None and ref.kind == "workspace_file":
                try:
                    stable = (
                        exc.content_hash in (None, before_hash)
                        and await run_index_io(self._hash, source.path) == before_hash
                    )
                except OSError:
                    stable = False
            if not stable:
                reason = "Original changed during indexing; retry required"
                self.ledger.mark(ref, "failed", completed=completed, error=reason)
                return {"status": "failed", "reason": reason}
            self.ledger.mark(ref, "excluded", error=exc.reason, verified_hash=before_hash)
            return {"status": "excluded", "reason": exc.reason}
        except (SourceUnavailable, MediaPreparationError) as exc:
            reason = getattr(exc, "reason", "source_unavailable")
            if isinstance(exc, SourceUnavailable):
                if reason in RETRYABLE_SOURCE_UNAVAILABLE_REASONS:
                    # Storage outages are retryable work, even when the
                    # current actor cannot read the source.  Persist both the
                    # actor attempt and a failed global checkpoint so the
                    # durable queue does not ACK the job as terminal.
                    self.ledger.attempt(ref, resolver.scope.user_id, "failed", reason)
                    self.ledger.mark(ref, "failed", completed=completed, error=reason)
                    return {"status": "failed", "reason": reason}
                if (not reuse_only and ref.kind in {"chat_attachment", "conversation_message"}
                        and await resolver.privacy_masked(ref)):
                    return await self._withdraw_private_source(ref)
                if not reuse_only and await self._source_is_globally_unavailable(
                    resolver, ref, reason
                ):
                    cleanup_reason = (
                        reason
                        if reason in GLOBAL_SOURCE_UNAVAILABLE_REASONS
                        else "source_deleted"
                    )
                    if (ref.kind == "workspace_file" and cleanup_reason == "workspace_file_not_indexable"
                            and await run_index_io(self.ledger.state, ref) in {None, "excluded"}):
                        # Points are written only after a ledger checkpoint;
                        # never-indexed or already-withdrawn Files need no
                        # vector store round trip (excluded/unsupported files).
                        self.ledger.mark(ref, "excluded", error=cleanup_reason)
                        return {"status": "excluded", "reason": cleanup_reason}
                    return await self._withdraw_source(ref, cleanup_reason)
                # An unauthorized attempt must never invalidate another user's
                # shared ready derivative or race its indexing progress.
                self.ledger.attempt(ref, resolver.scope.user_id, "excluded", reason)
            else:
                self.ledger.mark(ref, "failed", completed=completed, error=reason)
            return {
                "status": "excluded"
                if isinstance(exc, SourceUnavailable)
                else "failed",
                "reason": reason,
            }
        except Exception as exc:
            self.ledger.mark(ref, "failed", completed=completed, error=str(exc))
            return {"status": "failed", "reason": str(exc)}
        finally:
            try:
                if iterator is not None:
                    await run_index_io(iterator.close)
            finally:
                try:
                    await run_index_io(file_lock.release)
                finally:
                    lock.release()

    async def _withdraw_source(self, ref, reason):
        """Remove derivatives after the canonical source is globally gone."""
        self.ledger.mark(ref, "running", error="source_derivative_cleanup")
        if not await self.initialize(require_embedding=False):
            raise RuntimeError("Source derivative cleanup index unavailable")
        selector = models.Filter(must=[models.FieldCondition(
            key="source_key", match=models.MatchValue(value=ref.key))])
        try:
            await self._io(self.client.delete, self.collection_name,
                           models.FilterSelector(filter=selector), wait=True)
            remaining = await self._io(self.client.count, self.collection_name,
                                      count_filter=selector, exact=True)
            if remaining.count:
                raise RuntimeError("Source derivative deletion was not verified")
        except BaseException:
            self.ledger.mark(ref, "failed", error="source_derivative_cleanup_incomplete")
            raise
        self.ledger.mark(ref, "excluded", error=reason)
        # sync_source still holds both the in-process and cross-process source
        # locks. This proof covers this embedding identity's collection only;
        # the maintenance report separately requires exact all-collection counts.
        with self.ledger.connect() as db:
            record_cleanup_verified(db, ref.key, EMBEDDING_FINGERPRINT, remaining=remaining.count)
        return {"status": "excluded", "reason": reason}

    async def _withdraw_private_source(self, ref):
        """Withdraw only the server-marked source's indexed derivatives."""
        self.ledger.mark(ref, "running", error="privacy_derivative_cleanup")
        if not await self.initialize(require_embedding=False):
            raise RuntimeError("Private derivative cleanup index unavailable")
        selector = models.Filter(must=[models.FieldCondition(
            key="source_key", match=models.MatchValue(value=ref.key))])
        try:
            await self._io(self.client.delete, self.collection_name,
                           models.FilterSelector(filter=selector), wait=True)
            remaining = await self._io(self.client.count, self.collection_name,
                                      count_filter=selector, exact=True)
            if remaining.count:
                raise RuntimeError("Private derivative deletion was not verified")
        except BaseException:
            self.ledger.mark(ref, "failed", error="privacy_derivative_cleanup_incomplete")
            raise
        self.ledger.mark(ref, "excluded", error="privacy_masking_source")
        return {"status": "excluded", "reason": "privacy_masking_source"}

    async def _allowed(self, resolver, kinds=None, exclude_kinds=None):
        refs = await run_index_io(self.ledger.ready)
        if kinds is not None:
            refs = [ref for ref in refs if ref.kind in kinds]
        if exclude_kinds:
            refs = [ref for ref in refs if ref.kind not in exclude_kinds]
        return await resolver.allowed_refs(refs)

    async def search(
        self, resolver, *, query="", image_ref=None, query_page=1, limit=20, _prepared=None, kinds=None,
        exclude_kinds=None,
    ):
        allowed = await self._allowed(resolver, kinds, exclude_kinds)
        if not allowed:
            return []
        if not await self.initialize(require_embedding=False):
            raise RuntimeError(
                self.embedding.last_error or "EmbeddingGemma 2 unavailable"
            )
        prepared = _prepared is not None
        if prepared:
            from .media_query import prepared_dense_cosine, require_prepared
            dense = require_prepared(_prepared)
            query = _prepared.lexical_text
            # Prepared semantic queries intentionally use only the shared
            # dense vector.  Direct/legacy text search below retains its
            # hash-sparse hybrid path.
            sparse = None
        elif image_ref is not None:
            original = await resolver.resolve(image_ref)
            # Image queries are server-resolved originals, never arbitrary paths/URLs.
            iterator = self.preparation.iter_prepare(
                original.path,
                source_id=image_ref.key,
                metadata={**original.metadata, "file_name": original.title},
                mime_type=original.metadata.get("mime_type"),
            )
            selected = None
            try:
                while (chunk := await self._next_chunk(iterator)) is not None:
                    if (
                        chunk.modality == "image"
                        and chunk.metadata.get("page_number", 1) == query_page
                    ):
                        selected = chunk
                        break
            finally:
                await run_index_io(iterator.close)
            if selected is None:
                raise ValueError("Image query/page unavailable")
            vectors = await self.embedding.encode_media([selected.model_input])
            dense = vectors[0] if vectors else None
            if dense is None:
                raise RuntimeError(
                    self.embedding.last_error or "Image query embedding failed"
                )
        else:
            dense = await self.embedding.embed_query(query)
        if not prepared:
            sparse = self.sparse.encode(query)
        source_match = models.MatchAny(any=allowed)
        if self.config.qdrant.local_path:
            # Pydantic validation otherwise replaces the list subclass.
            # Local Qdrant tests membership for every point; avoid O(N**2).
            source_match.any = _LocalSourceValues(source_match.any)
        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="source_key", match=source_match
                ),
                models.FieldCondition(
                    key="embedding_fingerprint",
                    match=models.MatchValue(value=EMBEDDING_FINGERPRINT),
                ),
            ]
        )
        count = max(1, min(int(limit), MAX_SEARCH_RESULTS))
        prefetch = []
        if dense:
            prefetch.append(
                models.Prefetch(
                    query=dense, using="dense", filter=query_filter, limit=count * SEARCH_OVERFETCH
                )
            )
        if sparse is not None and sparse.indices:
            prefetch.append(
                models.Prefetch(
                    query=sparse, using="sparse", filter=query_filter, limit=count * SEARCH_OVERFETCH
                )
            )
        if not prefetch:
            return []
        options = dict(
            collection_name=self.collection_name,
            query_filter=query_filter,
            with_payload=True,
            limit=count * SEARCH_OVERFETCH,
        )
        if len(prefetch) == 1:
            options.update(query=prefetch[0].query, using=prefetch[0].using)
        else:
            options.update(
                prefetch=prefetch, query=models.FusionQuery(fusion=models.Fusion.RRF)
            )
        if prepared and dense:
            # The prepared path is dense-only.  Still request the stored named
            # vector and calculate cosine from it so partition-local Qdrant
            # scores can never leak into semantic ranking.
            options["with_vectors"] = ["dense"]
        response = await self._io(self.client.query_points, **options)
        hits = []
        current_sources = {}
        for point in response.points:
            payload = point.payload or {}
            ref = SourceRef(payload["source_kind"], payload["original_id"])
            if ref.key not in current_sources:
                try:
                    current = await resolver.resolve(ref)
                    current_sources[ref.key] = (
                        current, await run_index_io(self._hash, current.path)
                    )
                except (SourceUnavailable, OSError):
                    current_sources[ref.key] = None
            authorized = current_sources[ref.key]
            if authorized is None:
                continue
            current, current_hash = authorized
            # A request-local result check, separate from candidate SQL ACL.
            if current_hash != payload.get("content_hash"):
                continue
            semantic_score = None
            if prepared:
                vector = getattr(point, "vector", None)
                semantic_score = prepared_dense_cosine(
                    dense, vector.get("dense") if isinstance(vector, dict) else None
                )
                if semantic_score is None:
                    # An invalid saved dense vector is not a semantic hit.
                    continue
            hits.append(
                {
                    "unit_id": str(point.id),
                    "source_kind": ref.kind,
                    "source_id": ref.id,
                    "score": float(semantic_score if semantic_score is not None else point.score),
                    "title": current.title,
                    "modality": payload["modality"],
                    "page_number": payload.get("page_number"),
                    "start_seconds": payload.get("start_seconds"),
                    "end_seconds": payload.get("end_seconds"),
                    "text_start": payload.get("text_start"),
                    "text_end": payload.get("text_end"),
                    "text_length": payload.get("text_length"),
                    "original_url": current.metadata.get("original_url"),
                    "session_id": current.metadata.get("session_id"),
                    "message_id": current.metadata.get("message_id"),
                    "files_location": current.metadata.get("files_location"),
                    "text": decrypt_text_if_needed(
                        payload.get("lexical_text", ""), aad=f"media-index:{point.id}"
                    )[:2000],
                }
            )
            if prepared:
                hits[-1]["_query_content_hash"] = current_hash
                hits[-1]["_query_semantic_score"] = semantic_score
            if len(hits) >= count:
                break
        return hits

    async def search_prepared(self, resolver, unit, *, limit=20, kinds=None, exclude_kinds=None):
        from .media_query import require_prepared
        require_prepared(unit)
        return await self.search(resolver, limit=limit, _prepared=unit, kinds=kinds, exclude_kinds=exclude_kinds)

    async def authorize_prepared_hits(self, resolver, hits):
        originals = {}
        for hit in hits:
            ref = SourceRef(hit["source_kind"], hit["source_id"])
            if ref.key not in originals:
                try:
                    source = await resolver.resolve(ref)
                    originals[ref.key] = (source, await run_index_io(self._hash, source.path))
                except (SourceUnavailable, OSError):
                    originals[ref.key] = None
        return [hit for hit in hits if (current := originals.get(
            SourceRef(hit["source_kind"], hit["source_id"]).key)) is not None and
            current[1] == hit.get("_query_content_hash") and current[0].title == hit["title"]]

    async def read_original(self, resolver, unit_id):
        # A unit lookup needs its live canonical source ACL, not a corpus scan.
        # Indexed metadata is private until resolve authorizes the original.
        if not await self.initialize(require_embedding=False):
            raise SourceUnavailable("Source unavailable")
        records = await self._io(
            self.client.retrieve,
            self.collection_name,
            [str(uuid.UUID(unit_id))],
            with_vectors=False,
        )
        payload = records[0].payload if records else {}
        try:
            ref = SourceRef(payload["source_kind"], payload["original_id"])
        except (KeyError, TypeError):
            raise SourceUnavailable("Source unavailable") from None
        if (
            payload.get("source_key") != ref.key
            or payload.get("embedding_fingerprint") != EMBEDDING_FINGERPRINT
            or not await run_index_io(self.ledger.is_ready, ref)
        ):
            raise SourceUnavailable("Source unavailable")
        current = await resolver.resolve(ref)
        if await run_index_io(self._hash, current.path) != payload.get(
            "content_hash"
        ):
            raise SourceUnavailable("Original changed; reindex required")
        iterator = self._chunks(current)
        selected = None
        try:
            while (chunk := await self._next_chunk(iterator)) is not None:
                if chunk.id == unit_id:
                    selected = chunk
                    break
        finally:
            await run_index_io(iterator.close)
        if (selected is not None and ref.kind == "workspace_file" and payload.get("lexical_hash")
                and hashlib.sha256(selected.lexical_text.encode()).hexdigest() != payload["lexical_hash"]):
            # Same id but different text: the extraction route changed; never serve it.
            raise SourceUnavailable("Original changed; reindex required")
        if selected is not None:
            # A distant page/time unit can require long decoding. Recheck after
            # decoder cleanup, immediately before returning its evidence.
            current = await resolver.resolve(
                SourceRef(payload["source_kind"], payload["original_id"])
            )
            if (
                selected.metadata.get("content_hash") != payload.get("content_hash")
                or await run_index_io(self._hash, current.path)
                != payload.get("content_hash")
            ):
                raise SourceUnavailable("Original changed; reindex required")
            return current, selected
        raise SourceUnavailable("Source unit unavailable")


_media_index = None


def get_media_index_service():
    global _media_index
    if _media_index is None:
        _media_index = MediaIndexService()
    return _media_index
