"""Operator-only shared candidate. Never writes source SQL or input stores.

Docs/Knowledge use the canonical writer set; actor ACL is applied at search.
Memory/media use the union of current legal readers, never an actor's stale
set. Only dense results can cross the shadow boundary. No adoption/ACK API.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import hashlib
import io
import json
import math
from pathlib import Path
import pickle
import sqlite3
import uuid

import portalocker
from qdrant_client import QdrantClient, models
from qdrant_client.local.persistence import CollectionPersistence
from sqlalchemy import select

from .docs_index import DocsIndexService, _content_hash, _node_units
from .embedding import get_embedding, EmbeddingGemma2Embedding
from .embedding_identity import DIMENSION, EMBEDDING_FINGERPRINT, embedding_collection_name, document_input
from .index_io import run_index_io
from .media_index import MediaIndexService, MigrationLedger
from .shadow_migration import DocsShadowIndex, ShadowCheckpoint, _knowledge_rows, MAX_SHADOW_RECORDS
from ..knowledge.index_service import KnowledgeIndexService
from ..memory.cross_session_memory import _memory_embedding_hash, _canonical_character_slug, NO_PROJECT_SCOPE
from ..memory.models import DocsLibrary, KnowledgeNode, KnowledgeSource, KnowledgeChunk, ConversationMessage, User
from ..security.field_crypto import encrypt_text
from ..services.search_sources import SearchSourceResolver, SearchScope, SourceRef, SourceUnavailable


class CandidateCoverageError(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _CandidateResolver(SearchSourceResolver):
    @staticmethod
    def _file(root, relative):
        from ..services.storage_roots import lexical_path, relative_parts, assert_no_links
        from ..services.storage_io import StorageError
        try:
            target = lexical_path(root).joinpath(*relative_parts(relative))
            assert_no_links(target)
        except FileNotFoundError as exc:
            raise SourceUnavailable(reason="original_missing") from exc
        except (ValueError, StorageError) as exc:
            reason = ("original_path_invalid" if isinstance(exc, ValueError)
                      or exc.code in {"invalid_path", "invalid_media_path", "unsafe_storage_path", "protected_path"}
                      else "original_storage_failure")
            raise SourceUnavailable(reason=reason) from exc
        except OSError as exc:
            raise SourceUnavailable(reason="original_storage_failure") from exc
        import stat
        try:
            if not stat.S_ISREG(target.stat().st_mode):
                raise SourceUnavailable(reason="original_path_invalid")
        except FileNotFoundError as exc:
            raise SourceUnavailable(reason="original_missing") from exc
        except OSError as exc:
            raise SourceUnavailable(reason="original_storage_failure") from exc
        return target


@contextmanager
def _ro(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        yield db
    finally:
        db.close()


def valid_dense(vector):
    return (isinstance(vector, list) and len(vector) == DIMENSION
            and all(isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x) for x in vector)
            and abs(sum(x * x for x in vector) - 1) <= .01)


def _separate(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    if a == b or a in b.parents or b in a.parents:
        raise ValueError("Candidate paths must be separate from input/runtime stores and manifests")


class _PointUnpickler(pickle.Unpickler):
    # Local Qdrant 1.16 persists PointStruct pickles. Do not allow arbitrary
    # globals from a supplied store to execute while reading its vectors.
    def find_class(self, module, name):
        if module == "qdrant_client.http.models.models" and name in {"PointStruct", "SparseVector"}:
            return getattr(models, name)
        raise RuntimeError("Unsupported local Qdrant point serialization")


class ReadOnlyShadow:
    """Bound, quiescent local store read through read-only SQLite, never Qdrant.

    The ordinary Qdrant lock remains held for the provider's lifetime. No hot
    copy, lock removal, source migration, payload adoption, or ledger mutation.
    Reads are bounded to requested IDs, so seven large stores aren't loaded.
    """
    def __init__(self, manifest, source_binding, peers=()):
        self.manifest = Path(manifest).resolve()
        self.stack = ExitStack()
        try:
            with _ro(self.manifest) as db:
                row = db.execute("SELECT generation,target_path,actor,source_binding,identity FROM shadow_generation").fetchone()
            if row is None or row[3:] != (source_binding, EMBEDDING_FINGERPRINT):
                raise RuntimeError("Shadow source database/fingerprint binding mismatch")
            uuid.UUID(row[2])
            self.values, self.target = row, Path(row[1]).resolve()
            if str(self.target) != row[1]:
                raise RuntimeError("Shadow destination binding mismatch")
            if self.target.is_symlink():
                raise RuntimeError("Shadow destination link refused")
            marker = self.target / ShadowCheckpoint.target_marker_name
            if marker.is_symlink():
                raise RuntimeError("Shadow binding link refused")
            with _ro(marker) as db:
                bound = db.execute("SELECT generation,target_path,actor,source_binding,identity FROM shadow_target_binding").fetchone()
            if bound != row:
                raise RuntimeError("Shadow target store binding mismatch")
            peer = next((p for p in peers if p.target == self.target), None)
            if peer is not None and peer.values != row:
                raise RuntimeError("Shadow input manifests disagree on destination binding")
            if peer is None:
                lock = self.stack.enter_context((self.target / ".lock").open("rb"))
                portalocker.lock(lock, portalocker.LOCK_EX | portalocker.LOCK_NB)
                self.stack.callback(portalocker.unlock, lock)
            self.metadata = json.loads((self.target / "meta.json").read_text(encoding="utf-8"))
            self.record_sets = {}
            self.unit_manifest_key, self.unit_manifest = None, {}
        except BaseException:
            self.stack.close()
            raise

    def close(self):
        self.stack.close()

    def points(self, collection, ref, ids, expected_sets=None):
        with _ro(self.manifest) as db:
            ready = db.execute("SELECT 1 FROM embedding_sources WHERE source_key=? AND status='ready' AND identity=?",
                               (ref.key, EMBEDDING_FINGERPRINT)).fetchone()
            if not ready or collection not in self.metadata["collections"]:
                return []
            if ref.kind in {"docs_library", "knowledge_source"}:
                # READY alone is insufficient: require the bound verified unit
                # checkpoint as well as its persisted point and current hash.
                checkpoints = {identifier: db.execute(
                    "SELECT content_hash,embedding_fingerprint FROM shadow_units WHERE collection_name=? AND source_key=? AND unit_id=?",
                    (collection, ref.key, identifier)).fetchone() for identifier in ids}
                ids = [i for i in ids if checkpoints[i]]
            else:
                checkpoints = {}
        path = self.target / "collection" / collection / "storage.sqlite"
        if path.is_symlink() or self.target not in path.resolve().parents:
            raise RuntimeError("Shadow collection path refused")
        result = []
        with _ro(path) as db:
            for identifier in ids:
                row = db.execute("SELECT point FROM points WHERE id=?", (CollectionPersistence.encode_key(identifier),)).fetchone()
                if row:
                    point = _PointUnpickler(io.BytesIO(row[0])).load()
                    if str(point.id) != identifier:
                        raise RuntimeError("Shadow canonical point ID mismatch")
                    payload = point.payload or {}
                    if identifier in checkpoints and checkpoints[identifier] != (
                            payload.get("content_hash"), payload.get("embedding_fingerprint")):
                        payload["_candidate_checkpoint_invalid"] = True
                    if expected_sets and payload.get("node_id") in expected_sets:
                        digest = payload.get("content_hash")
                        cache_key = (collection, ref.key, digest)
                        if cache_key not in self.record_sets:
                            if len(self.record_sets) >= 128:
                                self.record_sets.pop(next(iter(self.record_sets)))
                            sets = {}
                            if self.unit_manifest_key != (collection, ref.key):
                                self.unit_manifest = {}
                                with _ro(self.manifest) as manifest:
                                    declared = manifest.execute("SELECT unit_id,content_hash FROM shadow_units WHERE collection_name=? AND source_key=?",
                                                                (collection, ref.key))
                                    for count, (unit_id, content_hash) in enumerate(declared, 1):
                                        if count > MAX_SHADOW_RECORDS:
                                            raise RuntimeError("Shadow unit manifest exceeds candidate bound")
                                        self.unit_manifest.setdefault(content_hash, []).append(unit_id)
                                self.unit_manifest_key = (collection, ref.key)
                            for unit_id in self.unit_manifest.get(digest, []):
                                raw = db.execute("SELECT point FROM points WHERE id=?", (CollectionPersistence.encode_key(unit_id),)).fetchone()
                                if raw:
                                    item = _PointUnpickler(io.BytesIO(raw[0])).load()
                                    sets.setdefault((item.payload or {}).get("node_id"), set()).add(str(item.id))
                            self.record_sets[cache_key] = sets
                        actual = self.record_sets[cache_key].get(payload.get("node_id"), set())
                        if actual != expected_sets[payload["node_id"]]:
                            payload["_candidate_checkpoint_invalid"] = True
                    result.append(point)
        return result


class CandidateCheckpoint:
    marker_name = ".aoitalk-candidate-binding.sqlite"

    def __init__(self, manifest, target, *, generation, source_binding, domain, shadows, format_version=1):
        from .runtime_transfer import CANDIDATE_FORMATS
        if format_version not in CANDIDATE_FORMATS:
            raise ValueError("Unsupported candidate format")
        self.target, self.manifest = Path(target).resolve(), Path(manifest).resolve()
        _separate(self.target, self.manifest)
        self.values = (CANDIDATE_FORMATS[format_version], generation, source_binding, str(self.target),
                       str(self.manifest), EMBEDDING_FINGERPRINT, domain,
                       json.dumps(sorted([list(s.values) + [str(s.manifest)] for s in shadows])))
        for shadow in shadows:
            _separate(self.target, shadow.target)
            _separate(self.manifest, shadow.target)
            _separate(self.target, shadow.manifest)
            _separate(self.manifest, shadow.manifest)
        self._check_existing(self.manifest, "candidate_generation")
        self.target.mkdir(parents=True, exist_ok=True)
        marker = self.target / self.marker_name
        if not marker.exists() and any(self.target.iterdir()):
            raise RuntimeError("Candidate destination is unbound and nonempty")
        created = False
        try:
            with marker.open("xb"):
                created = True
        except FileExistsError:
            pass
        if marker.is_symlink():
            raise RuntimeError("Candidate binding link refused")
        with sqlite3.connect(marker) as db:
            db.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables != {"candidate_generation"}:
                raise RuntimeError("Candidate destination binding invalid")
            row = db.execute("SELECT binding FROM candidate_generation").fetchone() if tables else None
            if not created and row is None:
                raise RuntimeError("Candidate destination binding missing")
            if row is not None and row[0] != json.dumps(self.values):
                raise RuntimeError("Candidate destination binding changed")
            db.execute("CREATE TABLE IF NOT EXISTS candidate_generation (singleton INTEGER PRIMARY KEY CHECK(singleton=1), binding TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO candidate_generation VALUES(1,?)", (json.dumps(self.values),))
        self.ledger = MigrationLedger(self.manifest)
        with self.ledger.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS candidate_generation (singleton INTEGER PRIMARY KEY CHECK(singleton=1), binding TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO candidate_generation VALUES(1,?)", (json.dumps(self.values),))
            db.execute("CREATE TABLE IF NOT EXISTS candidate_units (collection_name TEXT, source_key TEXT, unit_id TEXT, PRIMARY KEY(collection_name,unit_id))")
            columns = {r[1] for r in db.execute("PRAGMA table_info(candidate_units)")}
            for column in ("input_digest", "dense_digest"):
                if column not in columns:
                    db.execute(f"ALTER TABLE candidate_units ADD COLUMN {column} TEXT")

    def _check_existing(self, path, table):
        if not path.exists():
            return
        with _ro(path) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if table not in tables:
                raise RuntimeError("Existing non-candidate manifest refused")
            row = db.execute("SELECT binding FROM candidate_generation").fetchone()
            if row != (json.dumps(self.values),):
                raise RuntimeError("Candidate manifest binding changed")

    @contextmanager
    def writer(self):
        with (self.target / ".candidate-writer.lock").open("a+b") as handle:
            try:
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
            except portalocker.LockException as exc:
                raise RuntimeError("Candidate writer already running") from exc
            try:
                yield
            finally:
                portalocker.unlock(handle)


@dataclass
class Unit:
    point: object
    text: str = ""
    title: str = ""
    media_input: object = None
    record_units: object = None


def _dense(point):
    return point.vector.get("dense") if isinstance(point.vector, dict) else point.vector


def _dense_digest(vector):
    return hashlib.sha256(json.dumps(vector, separators=(",", ":")).encode()).hexdigest()


def _input_digest(unit):
    payload = {k: v for k, v in unit.point.payload.items() if k not in {"text", "lexical_text", "source_file"}}
    model_input = (document_input(unit.text, unit.title) if unit.media_input is None else
                   EmbeddingGemma2Embedding._pack_media(unit.media_input))
    return hashlib.sha256(json.dumps([str(unit.point.id), payload, model_input],
        sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _matches(previous, current):
    # Payload is always rebuilt. Only input/identity/locator fields establish
    # that a vector belongs to the current canonical unit.
    keys = ("embedding_fingerprint", "content_hash", "embedding_input_hash", "lexical_hash",
            "node_id", "docs_library_id", "input_version", "unit_kind", "derived_text_offset",
            "chunk_id", "source_id", "source_key", "doc_id", "modality", "original_id", "source_kind",
            "page_number", "frame_index", "start_seconds", "end_seconds", "text_offset", "title", "file_name",
            "memory_id", "memory_version", "owner_key", "scope_type", "scope_id", "original_updated_at")
    a, b = previous.payload or {}, current.payload or {}
    return (not a.get("_candidate_checkpoint_invalid") and str(previous.id) == str(current.id) and valid_dense(_dense(previous))
            and all(a.get(key) == b.get(key) for key in keys if key in b))


class RuntimeCandidate:
    def __init__(self, config, checkpoint, shadows):
        if not config.qdrant.local_path or Path(config.qdrant.local_path).resolve() != checkpoint.target:
            raise RuntimeError("Candidate config destination differs from its binding")
        self.config, self.checkpoint, self.shadows = config, checkpoint, shadows
        self.encoder = get_embedding(config.embedding)
        self.client = None
        self.stats = dict(reused=0, encoded=0, conflicts=0)
        self.reuse_diagnostics = dict.fromkeys((
            "checkpoint_missing", "source_or_input_changed", "payload_identity_mismatch",
            "dense_exact", "dense_sdk_repr_exact", "dense_unverified"), 0)
        self.batch_size = max(1, min(config.indexing.batch_size, config.embedding.batch_size, 32))

    async def io(self, function, *args, **kwargs):
        return await run_index_io(function, *args, **kwargs)

    async def batch(self, collection, ref, units, recheck):
        from .runtime_transfer import LOCAL_QDRANT_VERSION, TransferError, version
        if version("qdrant-client") != LOCAL_QDRANT_VERSION:
            raise TransferError("Unsupported local Qdrant serialization version")
        ids = [str(u.point.id) for u in units]
        if len(set(ids)) != len(ids):
            raise RuntimeError("Duplicate canonical unit IDs")
        old = {str(p.id): p for p in await self.io(self.client.retrieve, collection, ids, with_vectors=True)}
        input_digests = {str(u.point.id): await self.io(_input_digest, u) for u in units}
        with self.checkpoint.ledger.connect() as db:
            verified = {i: db.execute("SELECT source_key,input_digest,dense_digest FROM candidate_units WHERE collection_name=? AND unit_id=?",
                                      (collection, i)).fetchone() for i in ids}
        # A reopened Qdrant-local client exposes raw persisted cosine inputs,
        # whereas the checkpoint recorded the prior add/update retrieve value.
        # Verify the immutable stored PointStruct against the exact supported
        # SDK representations, rather than re-encoding an unchanged source.
        persisted_prefix = {}
        eligible = []
        for unit in units:
            identifier = str(unit.point.id)
            checkpoint, previous = verified[identifier], old.get(identifier)
            if checkpoint is None:
                self.reuse_diagnostics["checkpoint_missing"] += 1
            elif checkpoint[:2] != (ref.key, input_digests[identifier]):
                self.reuse_diagnostics["source_or_input_changed"] += 1
            elif previous is None or not _matches(previous, unit.point):
                self.reuse_diagnostics["payload_identity_mismatch"] += 1
            elif checkpoint[2] == _dense_digest(_dense(previous)):
                self.reuse_diagnostics["dense_exact"] += 1
            else:
                eligible.append(unit)
        if eligible:
            persisted_prefix = await self.io(
                self._verified_persisted_prefix, collection, eligible, verified
            )
            self.reuse_diagnostics["dense_sdk_repr_exact"] += len(persisted_prefix)
            self.reuse_diagnostics["dense_unverified"] += len(eligible) - len(persisted_prefix)
        offers = {identifier: [] for identifier in ids}
        expected_sets = {u.point.payload["node_id"]: u.record_units for u in units if u.record_units is not None}
        for shadow in self.shadows:
            for point in await self.io(shadow.points, collection, ref, ids, expected_sets):
                offers[str(point.id)].append(point)
        pending = []
        for index, unit in enumerate(units):
            point = unit.point
            candidates = offers[str(point.id)]
            # A disagreement is independent of actor order. Even one invalid
            # duplicate triggers fixed-model reconstruction, never last-wins.
            vectors = [_dense(p) for p in candidates]
            conflict = bool(candidates) and (any(not _matches(p, point) for p in candidates)
                                            or any(v != vectors[0] for v in vectors[1:]))
            if conflict:
                self.stats["conflicts"] += 1
            previous = old.get(str(point.id))
            checkpoint = verified[str(point.id)]
            verified_prefix = (previous is not None and _matches(previous, point) and checkpoint == (
                ref.key, input_digests[str(point.id)], _dense_digest(_dense(previous))))
            vector = (_dense(previous) if verified_prefix else
                      persisted_prefix.get(str(point.id)) if previous is not None and _matches(previous, point) else
                      vectors[0] if previous is None and candidates and not conflict else None)
            # A verified candidate prefix is authoritative on resume, even if
            # its source shadow was corrupt; it was built from current input.
            if vector is None:
                pending.append(index)
            else:
                self.stats["reused"] += 1
            if isinstance(point.vector, dict):
                point.vector["dense"] = vector or [1.] + [0.] * (DIMENSION - 1)
            else:
                point.vector = vector or [1.] + [0.] * (DIMENSION - 1)
        await recheck()
        text_ids = [i for i in pending if units[i].media_input is None]
        media_ids = [i for i in pending if units[i].media_input is not None]
        generated = []
        if text_ids:
            vectors = await self.encoder.embed([units[i].text for i in text_ids], titles=[units[i].title for i in text_ids])
            if len(vectors) != len(text_ids):
                raise RuntimeError("Candidate encoder count mismatch")
            generated += list(zip(text_ids, vectors))
        if media_ids:
            vectors = await self.encoder.encode_media([units[i].media_input for i in media_ids])
            if len(vectors) != len(media_ids):
                raise RuntimeError("Candidate encoder count mismatch")
            generated += list(zip(media_ids, vectors))
        if len(generated) != len(pending) or any(not valid_dense(v) for _, v in generated):
            raise RuntimeError("Candidate encoder returned invalid count/dimension/finite/L2")
        for index, vector in generated:
            point = units[index].point
            if isinstance(point.vector, dict):
                point.vector["dense"] = vector
            else:
                point.vector = vector
        self.stats["encoded"] += len(pending)
        await recheck()
        for unit in units:
            if not isinstance(unit.point.vector, dict):
                text = "" if unit.point.payload.get("source_kind") == "context_memory" else unit.text
                unit.point.payload["text"] = encrypt_text(text, aad=f"qdrant.payload.text:{unit.point.payload['doc_id']}")
        points = [u.point for u in units]
        await self.io(self.client.upsert, collection, points, wait=True)
        saved = await self.io(self.client.retrieve, collection, ids, with_vectors=True)
        expected = {str(p.id): p for p in points}
        if {str(p.id) for p in saved} != set(ids):
            raise RuntimeError("Candidate persisted unit set mismatch")
        for point in saved:
            current = expected[str(point.id)]
            norm = math.sqrt(sum(x * x for x in _dense(current)))
            same_dense = max(abs(a - b / norm) for a, b in zip(_dense(point), _dense(current))) <= 1e-5 if valid_dense(_dense(point)) else False
            if (not _matches(point, current) or not same_dense or point.payload != current.payload
                    or (isinstance(current.vector, dict) and point.vector.get("sparse") != current.vector.get("sparse"))):
                raise RuntimeError("Candidate persisted vector/payload/sparse verification failed")
        await recheck()
        with self.checkpoint.ledger.connect() as db:
            db.executemany("INSERT OR REPLACE INTO candidate_units (collection_name,source_key,unit_id,input_digest,dense_digest) VALUES(?,?,?,?,?)",
                           [(collection, ref.key, str(p.id), input_digests[str(p.id)], _dense_digest(_dense(p))) for p in saved])

    def _verified_persisted_prefix(self, collection, units, verified):
        from .runtime_transfer import checkpoint_vector, TransferError
        result = {}
        path = self.checkpoint.target / "collection" / collection / "storage.sqlite"
        if path.is_symlink() or self.checkpoint.target not in path.resolve().parents:
            raise RuntimeError("Candidate persisted collection path refused")
        with _ro(path) as db:
            for unit in units:
                identifier = str(unit.point.id)
                row = db.execute("SELECT point FROM points WHERE id=?",
                                 (CollectionPersistence.encode_key(identifier),)).fetchone()
                if row is None:
                    continue
                point = _PointUnpickler(io.BytesIO(row[0])).load()
                if not _matches(point, unit.point):
                    continue
                try:
                    result[identifier] = checkpoint_vector(point, verified[identifier][2])
                except TransferError as exc:
                    if str(exc) != "Candidate dense checkpoint checksum mismatch":
                        raise
                    # A damaged checksum must be rebuilt from canonical input.
        return result

    async def finish(self, collection, ref, retained):
        with self.checkpoint.ledger.connect() as db:
            stale = [r[0] for r in db.execute("SELECT unit_id FROM candidate_units WHERE collection_name=? AND source_key=?",
                                            (collection, ref.key)) if r[0] not in retained]
        # Scan the candidate source as well: an interrupted upsert can exist
        # before its SQLite checkpoint. Only this independently bound store.
        key = {"docs_library": "docs_library_id", "knowledge_source": "source_id",
               "conversation_message": "doc_id", "context_memory": "memory_id"}.get(ref.kind, "source_key")
        value = ref.id if key != "source_key" else ref.key
        offset = None
        while True:
            rows, offset = await self.io(self.client.scroll, collection, limit=1000, offset=offset,
                scroll_filter=models.Filter(must=[models.FieldCondition(key=key, match=models.MatchValue(value=value))]),
                with_payload=False, with_vectors=False)
            stale.extend(str(p.id) for p in rows if str(p.id) not in retained)
            if offset is None:
                break
        stale = sorted(set(stale))
        for start in range(0, len(stale), 1000):
            await self.io(self.client.delete, collection, models.PointIdsList(points=stale[start:start + 1000]), wait=True)
        with self.checkpoint.ledger.connect() as db:
            db.executemany("DELETE FROM candidate_units WHERE collection_name=? AND unit_id=?", [(collection, i) for i in stale])
        self.checkpoint.ledger.mark(ref, "ready", completed=len(retained), total=len(retained))

    async def source(self, collection, ref, build):
        ledger = self.checkpoint.ledger
        ledger.mark(ref, "running")
        try:
            units = await build()
            def signatures(values):
                return [(str(u.point.id), {k: v for k, v in u.point.payload.items() if k != "text"},
                         u.point.vector.get("sparse") if isinstance(u.point.vector, dict) else None) for u in values]
            signature = signatures(units)

            async def recheck():
                current = await build()
                actual = signatures(current)
                if actual != signature:
                    raise RuntimeError("Canonical source changed during candidate build; rerun")

            for start in range(0, len(units), self.batch_size):
                await self.batch(collection, ref, units[start:start + self.batch_size], recheck)
            await recheck()
            await self.finish(collection, ref, {str(u.point.id) for u in units})
            if not units and ref.kind == "conversation_message":
                ledger.mark(ref, "excluded", error="no_current_legal_reader_or_privacy_masking")
        except asyncio.CancelledError:
            ledger.mark(ref, "interrupted", error="cancelled; rerun verifies stored prefix")
            raise
        except BaseException:
            ledger.mark(ref, "failed", error="candidate_source_verification_failed")
            raise

    async def _docs_service(self):
        service = DocsIndexService(self.config)
        service.client, service._is_local_mode = self.client, True
        await self.io(service._ensure_collection)
        return service

    async def _docs_library_ids(self, session, service):
        ids = list((await session.execute(select(DocsLibrary.id).order_by(DocsLibrary.id))).scalars())
        return sorted(set(ids) | await service.indexed_library_ids())

    async def docs(self, session):
        service = await self._docs_service()
        ids = await self._docs_library_ids(session, service)
        for library_id in ids:
            ref = SourceRef("docs_library", str(library_id))
            self.checkpoint.ledger.mark(ref, "running")
            async def records(node_ids=None):
                stmt = select(KnowledgeNode.id, KnowledgeNode.title, KnowledgeNode.description, KnowledgeNode.project_id,
                    KnowledgeNode._body_text.label("body_text"), KnowledgeNode._body_json.label("body_json")).where(
                    KnowledgeNode.docs_library_id == library_id, KnowledgeNode.archived_at.is_(None)).order_by(KnowledgeNode.id)
                if node_ids is not None:
                    stmt = stmt.where(KnowledgeNode.id.in_(node_ids))
                cursor = None
                while True:
                    page = stmt.limit(1000) if cursor is None else stmt.where(KnowledgeNode.id > cursor).limit(1000)
                    rows = (await session.execute(page)).all()
                    if not rows:
                        break
                    for node, text, tags in await DocsShadowIndex._records(service, session, rows, library_id):
                        yield node, text, sorted(tags)
                    cursor = rows[-1].id

            def signature(node, text, tags):
                return (_content_hash(text), str(node.project_id), node.title, tuple(tags))

            pending, signatures, retained = [], {}, set()
            async def flush():
                if not pending:
                    return
                node_ids = {uuid.UUID(u.point.payload["node_id"]) for u in pending}
                async def recheck():
                    current = {node.id: signature(node, text, tags) async for node, text, tags in records(node_ids)}
                    if current != {i: signatures[i] for i in node_ids}:
                        raise RuntimeError("Docs canonical input changed during candidate batch; rerun")
                await self.batch(service.collection_name, ref, pending, recheck)
                retained.update(str(u.point.id) for u in pending)
                pending.clear()
            try:
                async for node, text, tags in records():
                    signatures[node.id] = signature(node, text, tags)
                    if len(signatures) > MAX_SHADOW_RECORDS:
                        raise RuntimeError("Candidate library exceeds canonical record bound")
                    node_units = list(_node_units(node.id, node.title, text))
                    unit_ids = {i for i, _, _ in node_units}
                    for point_id, unit_text, offset in node_units:
                        point = service._build_point(node, unit_text, tags, [1.] + [0.] * 767)
                        point.id = point_id
                        point.payload.update(content_hash=_content_hash(text), unit_kind="record" if offset == 0 else "span",
                                             derived_text_offset=offset)
                        pending.append(Unit(point, unit_text, node.title or "", record_units=unit_ids))
                        if len(pending) >= self.batch_size:
                            await flush()
                await flush()
                current = {node.id: signature(node, text, tags) async for node, text, tags in records()}
                if current != signatures:
                    raise RuntimeError("Docs canonical unit set changed during candidate; rerun")
                await self.finish(service.collection_name, ref, retained)
            except asyncio.CancelledError:
                self.checkpoint.ledger.mark(ref, "interrupted", error="cancelled; rerun verifies stored prefix")
                raise
            except BaseException:
                self.checkpoint.ledger.mark(ref, "failed", error="candidate_docs_verification_failed")
                raise

    async def _knowledge_service(self):
        service = KnowledgeIndexService(self.config)
        service.client, service._is_local_mode = self.client, True
        await self.io(service._ensure_collection)
        return service

    async def knowledge(self, session):
        service = await self._knowledge_service()
        ids = set((await session.execute(select(KnowledgeSource.id))).scalars())
        with self.checkpoint.ledger.connect() as db:
            ids.update(uuid.UUID(r[0]) for r in db.execute("SELECT source_id FROM embedding_sources WHERE kind='knowledge_source'"))
        for source_id in sorted(ids):
            ref = SourceRef("knowledge_source", str(source_id))
            self.checkpoint.ledger.mark(ref, "running")
            async def rows(identifiers=None, cursor=None):
                class Projection:
                    async def execute(self, statement):
                        statement = statement.order_by(None).order_by(KnowledgeChunk.id).limit(1000)
                        if identifiers is not None:
                            statement = statement.where(KnowledgeChunk.id.in_(identifiers))
                        if cursor is not None:
                            statement = statement.where(KnowledgeChunk.id > cursor)
                        return await session.execute(statement)
                return await _knowledge_rows(Projection(), source_id)

            def build(values):
                return [Unit(models.PointStruct(id=str(chunk.id), vector={"dense": [1.] + [0.] * 767,
                    "sparse": service.sparse_encoder.encode(service._sparse_text(chunk, document, source))},
                    payload=service._payload(chunk, document, source)), service._dense_text(chunk, document, source), document.title or "")
                    for chunk, document, source in values]

            def signatures(units):
                return {str(u.point.id): json.dumps(u.point.payload, sort_keys=True, ensure_ascii=False) for u in units}

            cursor, expected = None, {}
            try:
                while True:
                    page = await rows(cursor=cursor)
                    if not page:
                        break
                    for start in range(0, len(page), self.batch_size):
                        units = build(page[start:start + self.batch_size])
                        signature = signatures(units)
                        expected.update(signature)
                        if len(expected) > MAX_SHADOW_RECORDS:
                            raise RuntimeError("Candidate Knowledge source exceeds canonical record bound")
                        async def recheck():
                            current = signatures(build(await rows([uuid.UUID(i) for i in signature])))
                            if current != signature:
                                raise RuntimeError("Knowledge canonical input changed during candidate batch; rerun")
                        await self.batch(service.collection_name, ref, units, recheck)
                    cursor = page[-1][0].id
                cursor, current = None, {}
                while True:
                    page = await rows(cursor=cursor)
                    if not page:
                        break
                    current.update(signatures(build(page)))
                    if len(current) > MAX_SHADOW_RECORDS:
                        raise RuntimeError("Candidate Knowledge source exceeds canonical record bound")
                    cursor = page[-1][0].id
                if current != expected:
                    raise RuntimeError("Knowledge canonical unit set changed during candidate; rerun")
                await self.finish(service.collection_name, ref, set(expected))
            except asyncio.CancelledError:
                self.checkpoint.ledger.mark(ref, "interrupted", error="cancelled; rerun verifies stored prefix")
                raise
            except BaseException:
                self.checkpoint.ledger.mark(ref, "failed", error="candidate_knowledge_verification_failed")
                raise

    async def reader(self, session, ref, *, open_file=False):
        # Recompute active readers on every check; a revoked owner never
        # decides global deletion while another current reader is legal.
        cursor = None
        while True:
            stmt = select(User.id).where(User.is_active.is_(True)).order_by(User.id).limit(1000)
            if cursor is not None:
                stmt = stmt.where(User.id > cursor)
            ids = list((await session.execute(stmt)).scalars())
            if not ids:
                return None, None
            for actor in ids:
                resolver = SearchSourceResolver(session, SearchScope(actor))
                try:
                    source = await resolver.resolve(ref, open_file=open_file)
                    return resolver, source
                except SourceUnavailable:
                    continue
            cursor = ids[-1]

    @staticmethod
    async def media_reader(session, ref):
        """Formal exclusions precede originals; authorized read failures fail coverage."""
        from ..memory.models import (KnowledgeDocument, MeetingProcessingJob, GeneratedMedia,
                                     GenerationOutput, MediaAssetRevision, TaskAttachment, KnowledgeAttachment)
        from ..services.storage_io import StorageError
        from ..services.clip_ingest_storage import ClipUploadError
        models_by_kind = {"task_attachment": TaskAttachment, "docs_attachment": KnowledgeAttachment,
                          "knowledge_document": KnowledgeDocument, "meeting_audio": MeetingProcessingJob,
                          "generated_media": GeneratedMedia, "generation_output": GenerationOutput}
        model = models_by_kind.get(ref.kind)
        if model is not None and await session.scalar(select(model.id).where(model.id == uuid.UUID(ref.id))) is None:
            return None, None, "canonical_source_deleted"
        if ref.kind == "chat_attachment":
            row = (await session.execute(select(ConversationMessage.message_metadata, ConversationMessage.deleted_at,
                ConversationMessage.is_active_branch).where(ConversationMessage.id == uuid.UUID(ref.id.split(":")[0])))).first()
            if row is None or row.deleted_at is not None:
                return None, None, "canonical_source_deleted"
            from ..services.privacy_masking_projection import is_privacy_masking_source
            if is_privacy_masking_source(row.message_metadata):
                return None, None, "privacy_masking_source"
            if row.is_active_branch is False:
                return None, None, "inactive_conversation_branch"
            attachments = (row.message_metadata or {}).get("attachments") or []
            index = int(ref.id.split(":")[1])
            if index < 0 or index >= len(attachments):
                return None, None, "canonical_source_deleted"
        if ref.kind == "knowledge_document":
            row = (await session.execute(select(KnowledgeDocument.status, KnowledgeSource.source_type).join(
                KnowledgeSource, KnowledgeSource.id == KnowledgeDocument.source_id).where(KnowledgeDocument.id == uuid.UUID(ref.id)))).first()
            if row is None or row.status != "active":
                return None, None, "canonical_source_inactive_or_deleted"
            if row.source_type == "growi":
                return None, None, "remote_growi_has_no_local_media_original"
        if ref.kind == "meeting_audio":
            status = await session.scalar(select(MeetingProcessingJob.status).where(MeetingProcessingJob.id == uuid.UUID(ref.id)))
            if status == "succeeded":
                return None, None, "meeting_audio_discarded_after_success"
        if ref.kind == "generated_media":
            status = await session.scalar(select(GeneratedMedia.status).where(GeneratedMedia.id == uuid.UUID(ref.id)))
            if status != "succeeded":
                return None, None, "generated_media_not_succeeded"
        if ref.kind == "generation_output":
            output = await session.scalar(select(GenerationOutput.id).where(GenerationOutput.id == uuid.UUID(ref.id)))
            if output is None:
                return None, None, "canonical_source_deleted"
            revision = await session.scalar(select(MediaAssetRevision.id).where(MediaAssetRevision.generation_output_id == uuid.UUID(ref.id)).limit(1))
            if revision is None:
                return None, None, "generation_output_not_registered_as_managed_asset"
        cursor, failures = None, []
        # For chat, authorize the message independently of its attachment's
        # pathname. A namespace/path defect isn't an ACL revocation.
        acl_ref = SourceRef("conversation_message", ref.id.split(":")[0]) if ref.kind == "chat_attachment" else ref
        while True:
            stmt = select(User.id).where(User.is_active.is_(True)).order_by(User.id).limit(1000)
            if cursor is not None:
                stmt = stmt.where(User.id > cursor)
            ids = list((await session.execute(stmt)).scalars())
            if not ids:
                if failures:
                    # Never mask a new storage/permission failure as an approved gap.
                    unexpected = set(failures) - {"original_missing", "original_path_invalid"}
                    raise CandidateCoverageError(sorted(unexpected or set(failures))[0])
                return None, None, "no_current_legal_reader"
            for actor in ids:
                resolver = _CandidateResolver(session, SearchScope(actor))
                if acl_ref.key not in await resolver.allowed_refs([acl_ref], require_local_original=False):
                    continue
                try:
                    return resolver, await resolver.resolve(ref, open_file=True), None
                except (SourceUnavailable, StorageError, ClipUploadError, OSError) as exc:
                    # A racing revocation must not turn an unauthorized reader
                    # into an original-failure claim, nor hide another reader.
                    if acl_ref.key not in await resolver.allowed_refs([acl_ref], require_local_original=False):
                        continue
                    reason = exc.reason if isinstance(exc, SourceUnavailable) else "source_unavailable"
                    if reason == "source_unavailable":
                        # A disconnected StorageRoot can wrap ENOENT. Preserve
                        # its typed storage failure rather than approving a gap.
                        cause = exc if isinstance(exc, StorageError) else (exc.__cause__ or exc)
                        reason = ("original_missing" if isinstance(cause, FileNotFoundError) else
                                  "original_path_invalid" if isinstance(cause, StorageError)
                                  and cause.code in {"invalid_path", "invalid_media_path", "unsafe_storage_path", "protected_path"} else
                                  "original_resolution_failure")
                    failures.append(reason)
            cursor = ids[-1]

    async def _memory_collection(self):
        collection = embedding_collection_name("aoitalk_conversation_memory")
        if not await self.io(self.client.collection_exists, collection):
            await self.io(self.client.create_collection, collection, vectors_config=models.VectorParams(size=DIMENSION, distance=models.Distance.COSINE))
        return collection

    async def memory(self, session):
        collection = await self._memory_collection()
        cursor = None
        current = set()
        while True:
            stmt = select(ConversationMessage.id).order_by(ConversationMessage.id).limit(1000)
            if cursor is not None:
                stmt = stmt.where(ConversationMessage.id > cursor)
            ids = list((await session.execute(stmt)).scalars())
            if not ids:
                break
            for identifier in ids:
                ref = SourceRef("conversation_message", str(identifier))
                current.add(ref.key)
                async def build(ref=ref):
                    resolver, source = await self.reader(session, ref)
                    if source is None:
                        return []
                    conversation = await resolver._conversation(source.metadata["session_id"])
                    digest = _memory_embedding_hash(source.text, source.metadata["session_id"], conversation.user_id,
                                                  conversation.character_name, source.metadata.get("project_id"))
                    point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "aoitalk:" + ref.id))
                    # Ciphertext is randomized: generate once per source and
                    # compare current plaintext input via its canonical digest.
                    payload = {**source.metadata, "user_id": str(conversation.user_id), "embedding_input_hash": digest,
                        "character_name": _canonical_character_slug(conversation.character_name),
                        "project_id": source.metadata.get("project_id") or NO_PROJECT_SCOPE,
                        "doc_id": ref.id, "embedding_fingerprint": EMBEDDING_FINGERPRINT}
                    return [Unit(models.PointStruct(id=point_id, vector=[1.] + [0.] * 767, payload=payload), source.text,
                                 f"Conversation {source.metadata['session_id']}")]
                await self.source(collection, ref, build)
            cursor = ids[-1]
        await self.withdraw_deleted(collection, "conversation_message", current)

    async def withdraw_deleted(self, collection, kind, current):
        with self.checkpoint.ledger.connect() as db:
            refs = [SourceRef(kind, r[0]) for r in db.execute("SELECT source_id FROM embedding_sources WHERE kind=?", (kind,))]
        for ref in refs:
            if ref.key not in current:
                await self.finish(collection, ref, set())
                self.checkpoint.ledger.mark(ref, "excluded", error="canonical_source_deleted")

    async def _context_memory_collection(self):
        from ..memory.context_memory_index import COLLECTION_NAME
        if not await self.io(self.client.collection_exists, COLLECTION_NAME):
            await self.io(self.client.create_collection, COLLECTION_NAME,
                          vectors_config=models.VectorParams(size=DIMENSION, distance=models.Distance.COSINE))
        return COLLECTION_NAME

    async def context_memory(self, session):
        from ..memory.context_memory_index import binding, input_hash, globally_live, indexing_actor
        from ..memory.models import ContextMemory
        from ..services.scoped_memory_service import ScopedMemoryService, ScopedMemoryError
        collection = await self._context_memory_collection()
        cursor, current = None, set()
        while True:
            stmt = select(ContextMemory.id).order_by(ContextMemory.id).limit(100)
            if cursor is not None:
                stmt = stmt.where(ContextMemory.id > cursor)
            ids = list((await session.execute(stmt)).scalars())
            if not ids:
                break
            for identifier in ids:
                ref = SourceRef("context_memory", str(identifier))
                current.add(ref.key)
                async def build(identifier=identifier, ref=ref):
                    row = await session.get(ContextMemory, identifier, populate_existing=True)
                    if not await globally_live(session, row):
                        return []
                    actor = await indexing_actor(session, row)
                    service = ScopedMemoryService()
                    try:
                        await service._require_scope_permission(session, scope=service._scope_from_memory(row), actor_id=actor, write=False)
                    except ScopedMemoryError:
                        return []
                    payload = {**binding(row), "embedding_input_hash": input_hash(row),
                               "embedding_fingerprint": EMBEDDING_FINGERPRINT, "doc_id": ref.id}
                    point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "aoitalk:" + ref.id))
                    return [Unit(models.PointStruct(id=point_id, vector=[1.] + [0.] * 767, payload=payload),
                                 str(row.content or ""), row.title)]
                await self.source(collection, ref, build)
            cursor = ids[-1]
        await self.withdraw_deleted(collection, "context_memory", current)

    async def _media_service(self):
        service = MediaIndexService(self.config, ledger_path=self.checkpoint.manifest, client=self.client)
        service.embedding = self.encoder
        await self.io(service._ensure_collection)
        return service

    async def media(self, session):
        from ..memory.models import (GenerationOutput, GeneratedMedia, TaskAttachment, KnowledgeAttachment,
                                     KnowledgeDocument, MeetingProcessingJob)
        service = await self._media_service()
        specs = {"generation_output": GenerationOutput, "generated_media": GeneratedMedia,
                 "task_attachment": TaskAttachment, "docs_attachment": KnowledgeAttachment,
                 "knowledge_document": KnowledgeDocument, "meeting_audio": MeetingProcessingJob,
                 "chat_attachment": ConversationMessage}
        for kind, model in specs.items():
            cursor, current = None, set()
            while True:
                columns = [model.id, model.message_metadata] if kind == "chat_attachment" else [model.id]
                stmt = select(*columns).order_by(model.id).limit(100)
                if cursor is not None:
                    stmt = stmt.where(model.id > cursor)
                rows = (await session.execute(stmt)).all()
                if not rows:
                    break
                for row in rows:
                    identifiers = [f"{row.id}:{i}" for i, _ in enumerate((row.message_metadata or {}).get("attachments") or [])] if kind == "chat_attachment" else [str(row.id)]
                    for identifier in identifiers:
                        ref = SourceRef(kind, identifier)
                        current.add(ref.key)
                        await self.media_source(session, service, ref)
                cursor = rows[-1].id
            await self.withdraw_deleted(service.collection_name, kind, current)

    async def media_source(self, session, service, ref):
        from .media import MediaPreparationError
        ledger = self.checkpoint.ledger
        ledger.mark(ref, "running")
        iterator, source = None, None
        try:
            _, source, exclusion = await self.media_reader(session, ref)
            if source is None:
                await self.finish(service.collection_name, ref, set())
                ledger.mark(ref, "excluded", error=exclusion)
                return
            original_hash = await self.io(service._hash, source.path)
            metadata = (source.title, source.metadata, source.text, source.lexical_segments)

            async def recheck():
                _, fresh, _ = await self.media_reader(session, ref)
                if fresh is None:
                    raise RuntimeError("Media source/reader revoked during candidate; rerun")
                try:
                    if (await self.io(service._hash, fresh.path) != original_hash
                            or (fresh.title, fresh.metadata, fresh.text, fresh.lexical_segments) != metadata):
                        raise RuntimeError("Media current hash/metadata changed during candidate; rerun")
                finally:
                    if fresh._resource:
                        fresh._resource.cleanup()

            retained = set()
            iterator = service._chunks(source)
            while True:
                chunk = await service._next_chunk(iterator)
                if chunk is None:
                    break
                if chunk.id in retained:
                    raise RuntimeError("Duplicate canonical media unit")
                lexical_hash = hashlib.sha256((chunk.lexical_text + source.title).encode()).hexdigest()
                payload = {**chunk.metadata, "source_key": ref.key, "source_kind": ref.kind, "original_id": ref.id,
                    "modality": chunk.modality, "content_hash": original_hash, "lexical_hash": lexical_hash,
                    "embedding_fingerprint": EMBEDDING_FINGERPRINT,
                    "lexical_text": self._media_lexical_text(chunk.lexical_text, chunk.id)}
                point = models.PointStruct(id=chunk.id, vector={"dense": [1.] + [0.] * 767,
                    "sparse": service.sparse.encode(source.title + " " + chunk.lexical_text)}, payload=payload)
                await self.batch(service.collection_name, ref,
                    [Unit(point, chunk.lexical_text, source.title, None if chunk.modality == "text" else chunk.model_input)], recheck)
                retained.add(chunk.id)
            if not retained:
                raise RuntimeError("Media original has no indexable units")
            await recheck()
            await self.finish(service.collection_name, ref, retained)
        except asyncio.CancelledError:
            ledger.mark(ref, "interrupted", error="cancelled; rerun verifies stored prefix")
            raise
        except CandidateCoverageError as exc:
            # Preserve derivatives for inspection/retry. Do not falsely ACK
            # an authorized but missing/invalid original as a formal exclusion.
            ledger.mark(ref, "failed", error=exc.reason)
            return
        except MediaPreparationError as exc:
            ledger.mark(ref, "failed", error=exc.reason)
            return
        except BaseException:
            ledger.mark(ref, "failed", error="candidate_media_verification_failed")
            raise
        finally:
            if iterator is not None:
                await self.io(iterator.close)
            if source is not None and source._resource:
                source._resource.cleanup()

    def _media_lexical_text(self, text, unit_id):
        return encrypt_text(text, aad=f"media-index:{unit_id}")

    async def run(self, session, domain):
        global_ref = SourceRef("candidate", domain)
        self.checkpoint.ledger.mark(global_ref, "running")
        try:
            if not await self.encoder.initialize():
                raise RuntimeError("Candidate fixed encoder unavailable")
            def open_client():
                # Retain ownership even if cancellation arrives while the
                # constructor loads local collections in its executor thread.
                self.client = QdrantClient(path=str(self.checkpoint.target))
            await self.io(open_client)
            domains = ["docs", "knowledge", "memory", "media"]
            if self.checkpoint.values[0] == "shared_candidate_v2":
                domains.append("context_memory")
            for selected in (domains if domain == "all" else [domain]):
                await getattr(self, selected)(session)
            with self.checkpoint.ledger.connect() as db:
                incomplete = db.execute("SELECT 1 FROM embedding_sources WHERE identity=? AND kind!='candidate' AND status IN ('failed','running','interrupted') LIMIT 1",
                                        (EMBEDDING_FINGERPRINT,)).fetchone() is not None
            self.checkpoint.ledger.mark(global_ref, "failed" if incomplete else "ready",
                                        error="candidate_coverage_incomplete" if incomplete else None)
        except asyncio.CancelledError:
            self.checkpoint.ledger.mark(global_ref, "interrupted", error="cancelled; candidate not ready")
            raise
        except BaseException:
            self.checkpoint.ledger.mark(global_ref, "failed", error="candidate not ready; rerun required")
            raise
        finally:
            if self.client:
                await self.io(self.client.close)
                self.client = None
        return {"mode": "candidate", "candidate_ready": not incomplete, "source_database": "read_only_rollback", "stats": self.stats, "reuse_diagnostics": self.reuse_diagnostics,
                "progress": self.checkpoint.ledger.progress(), "production_cutover": "not_performed",
                "live_metadata": "not_acknowledged", "legacy_files": "outside_registered_candidate_inventory"}
