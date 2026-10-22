"""Offline, fail-closed candidate-to-server transport. Never activates a runtime.

The canonical proof is an operator evidence input produced while *all* canonical
and index writers remain stopped. This module verifies its binding and exact unit
coverage; it does not claim to acquire the application's writer stop itself.
Source stores are opened only with read-only SQLite under their ordinary locks.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import io
from importlib.metadata import version
import json
import math
from pathlib import Path
import pickle
import re
import sqlite3
from urllib.parse import urlsplit, urlunsplit
import uuid

import numpy as np
import portalocker
from qdrant_client import models
from qdrant_client.local.persistence import CollectionPersistence

from .embedding_identity import (
    DIMENSION, EMBEDDING_FINGERPRINT, EMBEDDING_IDENTITY, MODEL_ID,
    embedding_collection_name,
)

BATCH_SIZE = 128
MAX_PROOF_BYTES = 64 * 1024 * 1024
NUMERIC_TOLERANCE = 1e-5
LOCAL_QDRANT_VERSION = "1.16.2"
ALLOWED_GAPS = frozenset({"original_missing", "original_path_invalid"})
PROOF_SCHEMA = "aoitalk-canonical-cutover-proof-v1"
APPROVAL_SCHEMA = "aoitalk-approved-search-gaps-v1"
RECEIPT_SCHEMA = "aoitalk-runtime-transfer-v1"
MARKER = ".aoitalk-candidate-binding.sqlite"


class TransferError(RuntimeError):
    """Messages are safe aggregates, never source IDs, metadata or paths."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def dense_digest(value):
    # Exactly runtime_candidate._dense_digest's serialization contract.
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _require(condition, message):
    if not condition:
        raise TransferError(message)


def read_json(path):
    path = Path(path)
    _require(not path.is_symlink() and path.stat().st_size <= MAX_PROOF_BYTES,
             "Evidence file is linked or exceeds the bounded evidence size")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "Evidence has duplicate JSON keys")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def endpoint_identity(url):
    parsed = urlsplit(url)
    _require(parsed.scheme in {"http", "https"} and parsed.hostname and
             not parsed.username and not parsed.password and not parsed.query and
             not parsed.fragment and parsed.path in {"", "/"},
             "Target must be an explicit credential-free Qdrant HTTP endpoint")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    return urlunsplit((parsed.scheme, f"{host}:{port}", "", "", ""))


@dataclass(frozen=True)
class CollectionPolicy:
    domain: str
    fingerprint: str
    input_version: object = None


CANDIDATE_FORMATS = {1: "shared_candidate_v1", 2: "shared_candidate_v2"}


def candidate_format_version(manifest):
    """Read the explicit format; never infer coverage from extra collections."""
    with _ro(manifest) as db:
        row = db.execute("SELECT binding FROM candidate_generation WHERE singleton=1").fetchone()
    _require(row is not None, "Candidate binding is missing")
    binding = json.loads(row[0])
    _require(isinstance(binding, list) and len(binding) == 8, "Invalid candidate binding")
    for number, tag in CANDIDATE_FORMATS.items():
        if binding[0] == tag:
            return number
    raise TransferError("Unsupported candidate format")


def collection_policies(docs_base="aoitalk_docs", knowledge_base="aoitalk_knowledge", *, format_version=1):
    """Fixed model + canonical input versions. Tests pin these to writer code.

    No CLI option can override model, fingerprints, dimension or tolerance.
    Collection base names are the only configurable names, as in RagConfig.
    """
    docs = hashlib.sha256(json.dumps([EMBEDDING_IDENTITY, MODEL_ID, DIMENSION, 4],
                                    separators=(",", ":")).encode()).hexdigest()
    knowledge = digest({"encoder": EMBEDDING_IDENTITY, "model": MODEL_ID,
                        "dimension": DIMENSION, "input_version": "knowledge-context-v1"})
    result = {
        embedding_collection_name(docs_base): CollectionPolicy("docs", docs, 4),
        embedding_collection_name(knowledge_base): CollectionPolicy("knowledge", knowledge),
        embedding_collection_name("aoitalk_media"): CollectionPolicy("media", EMBEDDING_FINGERPRINT),
        embedding_collection_name("aoitalk_conversation_memory"): CollectionPolicy("memory", EMBEDDING_FINGERPRINT),
    }
    _require(format_version in CANDIDATE_FORMATS, "Unsupported candidate format")
    if format_version == 2:
        from ..memory.context_memory_index import COLLECTION_NAME, INPUT_VERSION
        result[COLLECTION_NAME] = CollectionPolicy("context_memory", EMBEDDING_FINGERPRINT, INPUT_VERSION)
    _require(len(result) == (4 if format_version == 1 else 5), "Collection domains collide")
    for name in result:
        _require(re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", name) is not None,
                 "Invalid collection name")
    return result


def input_identity(policies):
    return digest({name: {"domain": p.domain, "fingerprint": p.fingerprint,
                          "input_version": p.input_version} for name, p in policies.items()})


def _dense(point):
    return point.vector.get("dense") if isinstance(point.vector, dict) else point.vector


def validate_point(point, policy):
    vector = _dense(point)
    _require(isinstance(vector, list) and len(vector) == DIMENSION and
             all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in vector)
             and abs(sum(v * v for v in vector) - 1) <= .01,
             "Dense vector failed dimension/finite/L2 verification")
    try:
        _require(str(uuid.UUID(str(point.id))) == str(point.id), "Noncanonical point ID")
    except ValueError as exc:
        raise TransferError("Noncanonical point ID") from exc
    payload = point.payload
    _require(isinstance(payload, dict) and payload.get("embedding_fingerprint") == policy.fingerprint,
             "Collection-specific embedding fingerprint mismatch")
    if policy.input_version is not None:
        _require(payload.get("input_version") == policy.input_version, "Canonical input version mismatch")
    if policy.domain in {"memory", "context_memory"}:
        _require(isinstance(point.vector, list), "Memory requires an unnamed dense vector")
        if policy.domain == "context_memory":
            _require(payload.get("source_kind") == "context_memory"
                     and payload.get("memory_id") == payload.get("doc_id")
                     and type(payload.get("memory_version")) is int and payload["memory_version"] >= 1
                     and _hash(payload.get("embedding_input_hash")), "ContextMemory canonical binding missing")
    else:
        _require(isinstance(point.vector, dict) and set(point.vector) == {"dense", "sparse"},
                 "Named dense/sparse vector layout mismatch")
        sparse = point.vector["sparse"]
        _require(isinstance(sparse, models.SparseVector) and len(sparse.indices) == len(sparse.values)
                 and len(set(sparse.indices)) == len(sparse.indices)
                 and all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < 2**32 for i in sparse.indices)
                 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in sparse.values),
                 "Sparse vector failed verification")
    return vector


def checkpoint_vector(point, checkpoint_digest):
    """Replay 1.16 local persistence representations; require an exact checksum.

    SQLite stores the original input PointStruct, but add/update retrieve returns
    differently rounded cosine values. Loading the closed store returns raw
    values. Only those deterministic representations may satisfy a checkpoint.
    """
    _require(version("qdrant-client") == LOCAL_QDRANT_VERSION, "Unsupported local Qdrant serialization version")
    raw = _dense(point)
    array32, array64 = np.array(raw, dtype=np.float32), np.array(raw, dtype=np.float64)
    candidates = [raw, (array32 / np.linalg.norm(array32)).tolist(),
                  (array64 / np.linalg.norm(array64)).astype(np.float32).tolist(),
                  (array64 / np.linalg.norm(array64)).tolist()]
    for vector in candidates:
        if dense_digest(vector) == checkpoint_digest:
            return vector
    raise TransferError("Candidate dense checkpoint checksum mismatch")


def _source_key(point, policy):
    payload = point.payload
    fields = {"docs": ("docs_library", "docs_library_id"),
              "knowledge": ("knowledge_source", "source_id"),
              "memory": ("conversation_message", "doc_id"),
              "context_memory": ("context_memory", "memory_id")}
    if policy.domain == "media":
        _require(payload.get("source_key") == f"{payload.get('source_kind')}:{payload.get('original_id')}",
                 "Media source locator mismatch")
        return payload.get("source_key")
    kind, field = fields[policy.domain]
    _require(isinstance(payload.get(field), str) and payload[field], "Canonical source locator missing")
    return kind + ":" + payload[field]


def compare_points(source, actual, policy):
    expected = validate_point(source, policy)
    received = validate_point(actual, policy)
    _require(str(source.id) == str(actual.id) and digest(source.payload) == digest(actual.payload),
             "Persisted point ID/payload mismatch")
    norm = math.sqrt(sum(v * v for v in expected))
    _require(max(abs(a - b / norm) for a, b in zip(received, expected)) <= NUMERIC_TOLERANCE,
             "Persisted dense numerical difference exceeds fixed tolerance")
    if policy.domain not in {"memory", "context_memory"}:
        left, right = source.vector["sparse"], actual.vector["sparse"]
        _require({i: float(np.float32(v)) for i, v in zip(left.indices, left.values)} ==
                 {i: float(np.float32(v)) for i, v in zip(right.indices, right.values)},
                 "Persisted sparse vector mismatch")


@contextmanager
def _ro(path):
    path = Path(path)
    _require(path.is_file() and not path.is_symlink(), "Source database is missing or linked")
    # Reject journals before the first connection: a mode=ro WAL connection
    # can otherwise create/update -shm even when no SQL writes are requested.
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        _require(not sidecar.is_symlink() and (not sidecar.exists() or sidecar.stat().st_size == 0),
                 "Candidate has an unreconciled SQLite journal")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


class _PointUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module == "qdrant_client.http.models.models" and name in {"PointStruct", "SparseVector"}:
            return getattr(models, name)
        raise TransferError("Unsupported local Qdrant point serialization")


def _lock(stack, path, *, existing=False):
    _require(not path.is_symlink(), "Writer lock is linked")
    handle = stack.enter_context(path.open("rb" if existing else "a+b"))
    try:
        portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except portalocker.LockException as exc:
        raise TransferError("Another candidate/transfer writer is active") from exc
    stack.callback(portalocker.unlock, handle)


def _separate(a, b):
    _require(a != b and a not in b.parents and b not in a.parents,
             "Input, output and evidence paths must be separate")


class ReadOnlyCandidate:
    """Immutable provider, never constructs QdrantClient for an input store."""
    def __init__(self, candidate, manifest, generation, source_binding, policies):
        _require(version("qdrant-client") == LOCAL_QDRANT_VERSION, "Unsupported local Qdrant serialization version")
        self.target, self.manifest = Path(candidate).absolute(), Path(manifest).absolute()
        self.policies, self.stack = policies, ExitStack()
        for path in (self.target, self.manifest):
            _require(path == path.resolve() and not path.is_symlink(), "Candidate path is linked or noncanonical")
        _separate(self.target, self.manifest)
        try:
            _lock(self.stack, self.target / ".candidate-writer.lock", existing=True)
            _lock(self.stack, self.target / ".lock", existing=True)
            with _ro(self.manifest) as db:
                row = db.execute("SELECT binding FROM candidate_generation WHERE singleton=1").fetchone()
            _require(row is not None, "Candidate binding is missing")
            self.binding = json.loads(row[0])
            format_version = 2 if any(p.domain == "context_memory" for p in policies.values()) else 1
            _require(len(self.binding) == 8 and self.binding[:7] == [
                CANDIDATE_FORMATS[format_version], generation, source_binding, str(self.target),
                str(self.manifest), EMBEDDING_FINGERPRINT, "all"], "Candidate source/generation/domain binding mismatch")
            with _ro(self.target / MARKER) as db:
                target_row = db.execute("SELECT binding FROM candidate_generation WHERE singleton=1").fetchone()
            _require(target_row == row, "Candidate marker/manifest binding mismatch")
            metadata = read_json(self.target / "meta.json")
            _require(not metadata.get("aliases") and set(metadata["collections"]) == set(policies),
                     "Candidate collections must exactly match its explicit format without aliases")
            self.configs = metadata["collections"]
            for name, policy in policies.items():
                self.validate_config(self.configs[name], policy)
            self.snapshot = self.snapshot_digest()
        except BaseException:
            self.stack.close()
            raise

    def close(self):
        self.stack.close()

    @staticmethod
    def validate_config(config, policy):
        params = models.CreateCollection.model_validate({k: v for k, v in config.items()
                                                         if k in models.CreateCollection.model_fields})
        vectors = params.vectors
        if policy.domain in {"memory", "context_memory"}:
            _require(isinstance(vectors, models.VectorParams) and not params.sparse_vectors,
                     "Memory collection schema mismatch")
            dense = vectors
        else:
            _require(isinstance(vectors, dict) and set(vectors) == {"dense"}
                     and set(params.sparse_vectors or {}) == {"sparse"}, "Dense/sparse collection schema mismatch")
            dense = vectors["dense"]
            _require(params.sparse_vectors["sparse"].modifier is None, "Sparse modifier must preserve stored scores")
        _require(dense.size == DIMENSION and dense.distance == models.Distance.COSINE
                 and dense.multivector_config is None and dense.quantization_config is None
                 and dense.datatype in {None, models.Datatype.FLOAT32}, "Dense collection schema mismatch")

    def snapshot_digest(self):
        paths = [("manifest", self.manifest), ("binding", self.target / MARKER),
                 ("metadata", self.target / "meta.json")]
        paths += [(name, self.target / "collection" / name / "storage.sqlite") for name in sorted(self.policies)]
        hashes = []
        for role, path in paths:
            _require(path.resolve() == path and path.is_file(), "Candidate snapshot is missing or linked")
            for suffix in ("-wal", "-journal"):
                sidecar = Path(str(path) + suffix)
                _require(not sidecar.exists() or sidecar.stat().st_size == 0, "Candidate has an unreconciled SQLite journal")
            h = hashlib.sha256()
            with path.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    h.update(block)
            hashes.append([role, h.hexdigest()])
        return digest(hashes)

    def pages(self, collection):
        path = self.target / "collection" / collection / "storage.sqlite"
        with _ro(path) as db, _ro(self.manifest) as manifest:
            cursor = db.execute("SELECT id,point FROM points ORDER BY id")
            while rows := cursor.fetchmany(BATCH_SIZE):
                output = []
                for encoded_id, raw in rows:
                    point = _PointUnpickler(io.BytesIO(raw)).load()
                    _require(isinstance(point, models.PointStruct) and
                             encoded_id == CollectionPersistence.encode_key(point.id), "Persisted ID encoding mismatch")
                    validate_point(point, self.policies[collection])
                    row = manifest.execute("SELECT source_key,input_digest,dense_digest FROM candidate_units "
                                           "WHERE collection_name=? AND unit_id=?", (collection, str(point.id))).fetchone()
                    _require(row is not None and all(_hash(v) for v in row[1:]), "Candidate verified input/dense checkpoint missing")
                    _require(row[0] == _source_key(point, self.policies[collection]), "Point/checkpoint source mismatch")
                    verified = checkpoint_vector(point, row[2])
                    if isinstance(point.vector, dict):
                        point.vector["dense"] = verified
                    else:
                        point.vector = verified
                    output.append((point, row))
                yield output

    def assert_unchanged(self):
        _require(self.snapshot_digest() == self.snapshot, "Candidate input changed during verification")


def proof_binding(source, code_sha):
    return {"generation": source.binding[1], "source_binding": source.binding[2],
            "code_sha": code_sha, "qdrant_local_version": LOCAL_QDRANT_VERSION,
            "embedding_fingerprint": EMBEDDING_FINGERPRINT,
            "input_identity": input_identity(source.policies),
            "candidate_binding_digest": digest(source.binding), "candidate_snapshot_digest": source.snapshot}


def validate_canonical_proof(proof, source, code_sha):
    _require(re.fullmatch(r"[0-9a-f]{40}", code_sha) is not None, "Explicit code SHA is required")
    _require(proof.get("schema") == PROOF_SCHEMA and proof.get("binding") == proof_binding(source, code_sha),
             "Fresh canonical proof binding mismatch")
    _require(proof.get("all_writers_stopped") is True and proof.get("canonical_rechecked") is True
             and proof.get("deleted_units_reconciled") is True and isinstance(proof.get("freeze_id"), str)
             and bool(proof["freeze_id"]), "Fresh canonical writer-freeze/reconciliation evidence missing")
    sources = proof.get("sources")
    units = proof.get("units")
    _require(isinstance(sources, list) and isinstance(units, list), "Canonical source/unit inventory missing")
    expected_sources = {}
    for item in sources:
        key = item.get("source_key")
        _require(isinstance(key, str) and ":" in key and key not in expected_sources
                 and _hash(item.get("registration_metadata_hash")), "Canonical source inventory invalid")
        _require(set(item) == {"source_key", "status", "reason", "registration_metadata_hash"},
                 "Canonical source evidence must contain only identity/status/digests")
        _require(item["status"] in {"ready", "excluded", "failed"}, "Unfinished canonical source")
        if item["status"] == "failed":
            _require(item["reason"] in ALLOWED_GAPS, "Unapproved failure class in canonical inventory")
        expected_sources[key] = item
    expected_units = {}
    for item in units:
        _require(isinstance(item, list) and len(item) == 4 and item[0] in source.policies
                 and item[1] in expected_sources and expected_sources[item[1]]["status"] == "ready"
                 and _hash(item[3]), "Canonical unit inventory invalid")
        key = (item[0], item[2])
        _require(key not in expected_units, "Duplicate canonical unit")
        expected_units[key] = (item[1], item[3])
    source_counts = {}
    for key, _ in expected_units.values():
        source_counts[key] = source_counts.get(key, 0) + 1
    with _ro(source.manifest) as db:
        actual_sources = {}
        global_rows = []
        for key, kind, identity, status, error, completed, total in db.execute(
            "SELECT source_key,kind,identity,status,error,completed,total FROM embedding_sources"):
            _require(identity == EMBEDDING_FINGERPRINT, "Mixed model identities in candidate ledger")
            if kind == "candidate":
                global_rows.append((key, status, error))
                continue
            item = expected_sources.get(key)
            _require(item is not None and item["status"] == status and item["reason"] == error,
                     "Canonical source coverage/status changed")
            if status == "ready":
                expected_count = source_counts.get(key, 0)
                _require(completed == total == expected_count, "Ready source unit count mismatch")
            actual_sources[key] = item
        _require(actual_sources == expected_sources, "Canonical source coverage mismatch")
        gaps = [v for v in sources if v["status"] == "failed"]
        _require(global_rows == [("candidate:all", "failed" if gaps else "ready",
                                  "candidate_coverage_incomplete" if gaps else None)],
                 "Global candidate completion checkpoint missing")
        actual_units = {(c, i): (s, d) for c, s, i, d in db.execute(
            "SELECT collection_name,source_key,unit_id,input_digest FROM candidate_units")}
        _require(actual_units == expected_units, "Candidate/canonical unit set or input digest mismatch")
    seen = set()
    for name in source.policies:
        for page in source.pages(name):
            for point, checkpoint in page:
                key = (name, str(point.id))
                _require(key not in seen and expected_units.get(key) == checkpoint[:2],
                         "Candidate persisted coverage differs from canonical units")
                seen.add(key)
    _require(seen == set(expected_units), "Candidate persisted unit is missing")
    source.assert_unchanged()
    return gaps


def approval_request(proof):
    """Creates only a request body. It never creates approval receipt evidence."""
    return {"schema": APPROVAL_SCHEMA, "binding": dict(proof["binding"]),
            "gaps": sorted([dict(v) for v in proof["sources"] if v["status"] == "failed"], key=lambda v: v["source_key"]),
            "preserve_originals_and_registrations": True}


def validate_approved_gaps(proof, manifest, approval_receipt):
    gaps = [v for v in proof["sources"] if v["status"] == "failed"]
    if not gaps and manifest is None and approval_receipt is None:
        return {"approved_gap_count": 0, "approval_digest": None}
    _require(isinstance(manifest, dict) and isinstance(approval_receipt, dict),
             "Exact missing-source manifest and explicit received approval evidence are required")
    # Repairs must regenerate/reapprove the manifest after ordinary ready proofs;
    # a missing old gap is never silently counted as a successful repair.
    _require(manifest == approval_request(proof), "Approved gaps differ in source/reason/metadata/binding")
    _require(all(v["reason"] in ALLOWED_GAPS for v in gaps), "New failure class is not approvable")
    _require(approval_receipt.get("schema") == "aoitalk-gap-approval-receipt-v1"
             and approval_receipt.get("manifest_checksum") == digest(manifest)
             and approval_receipt.get("generation") == proof["binding"]["generation"]
             and isinstance(approval_receipt.get("approval_reference"), str)
             and bool(approval_receipt["approval_reference"].strip())
             and approval_receipt.get("explicitly_received") is True,
             "Approval receipt does not explicitly approve this exact manifest")
    return {"approved_gap_count": len(gaps), "approval_digest": digest(approval_receipt)}


class TransferReceipt:
    """Only this output SQLite changes. Verified prefixes are rechecked on resume."""
    def __init__(self, path, binding):
        self.path = Path(path).absolute()
        _require(self.path == self.path.resolve() and not self.path.is_symlink(), "Receipt path is linked")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stack = ExitStack()
        try:
            _lock(self.stack, Path(str(self.path) + ".lock"))
            self.db = sqlite3.connect(self.path)
            self.stack.callback(self.db.close)
            tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            _require(not tables or tables == {"transfer", "verified_points"}, "Existing non-transfer receipt refused")
            self.db.execute("CREATE TABLE IF NOT EXISTS transfer(singleton INTEGER PRIMARY KEY CHECK(singleton=1), binding TEXT NOT NULL, state TEXT NOT NULL, report TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS verified_points(collection_name TEXT, unit_id TEXT, input_digest TEXT NOT NULL, dense_digest TEXT NOT NULL, point_digest TEXT NOT NULL, PRIMARY KEY(collection_name,unit_id))")
            row = self.db.execute("SELECT binding FROM transfer WHERE singleton=1").fetchone()
            encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"))
            _require(row is None or row == (encoded,), "Receipt source/target/evidence/code binding changed")
            self.db.execute("INSERT OR IGNORE INTO transfer VALUES(1,?,'incomplete',NULL)", (encoded,))
            self.db.commit()
        except BaseException:
            self.stack.close()
            raise

    def close(self):
        self.stack.close()

    def mark(self, state, report=None):
        self.db.execute("UPDATE transfer SET state=?,report=? WHERE singleton=1", (state, json.dumps(report) if report else None))
        self.db.commit()


def point_digest(point):
    return digest(point.model_dump(mode="json", include={"id", "payload", "vector"}))


def _retrieve(client, collection, expected, policy):
    result = client.retrieve(collection, ids=[p.id for p in expected], with_payload=True, with_vectors=True)
    actual = {str(p.id): p for p in result}
    _require(len(actual) == len(result) and set(actual) == {str(p.id) for p in expected},
             "Target retrieve returned a subset, duplicate or extra point")
    for point in expected:
        compare_points(point, actual[str(point.id)], policy)
    return actual


def validate_output_paths(source, outputs, evidence_paths=()):
    """Protect input files, sidecars, provider stores and hardlinks before writes."""
    protected = [source.target, source.manifest, source.target / MARKER, source.target / "meta.json"]
    protected += [source.target / "collection" / name / "storage.sqlite" for name in source.policies]
    providers = json.loads(source.binding[7])
    _require(isinstance(providers, list), "Candidate provider binding invalid")
    for provider in providers:
        _require(isinstance(provider, list) and len(provider) == 6, "Candidate provider binding invalid")
        protected += [Path(provider[1]).absolute(), Path(provider[5]).absolute()]
    protected += [Path(p).absolute() for p in evidence_paths]
    protected += [Path(str(p) + suffix) for p in list(protected) for suffix in ("-wal", "-shm", "-journal")]
    for output in outputs:
        output = Path(output).absolute()
        _require(output == output.resolve() and not output.is_symlink(), "Output path is linked or noncanonical")
        for path in protected:
            _separate(output, path)
            _require(not (output.exists() and path.exists() and output.samefile(path)), "Output aliases an input file")


def transfer(source, client, *, target_endpoint, receipt_path, canonical_proof,
             code_sha, approval_manifest=None, approval_receipt=None, confirm_all_writers_stopped=False,
             evidence_paths=()):
    """Transfer into an explicit endpoint client; no activation/delete/config API.

    Caller must keep every writer stopped from canonical proof through completion.
    A shared per-endpoint operator lock prevents independent receipt-path writers.
    Client endpoint is checked, rather than trusting a URL label for the receipt.
    """
    _require(confirm_all_writers_stopped is True, "Every canonical/index writer must remain stopped")
    endpoint = endpoint_identity(target_endpoint)
    # Public QdrantClient exposes the selected transport in init_options.
    options = getattr(client, "init_options", {})
    _require(options.get("url") and endpoint_identity(options["url"]) == endpoint,
             "Client target differs from the explicit server endpoint")
    receipt_path = Path(receipt_path).absolute()
    locks = Path(__file__).resolve().parents[2] / "data" / "search" / "transfer-locks"
    receipt_outputs = [receipt_path] + [Path(str(receipt_path) + suffix)
                                        for suffix in (".lock", "-journal", "-wal", "-shm")]
    validate_output_paths(source, receipt_outputs + [locks, locks / (digest(endpoint) + ".lock")], evidence_paths)
    with ExitStack() as stack:
        # One deterministic lock location per endpoint, independent of receipt.
        # This lock is local to this operator host, not a distributed server lease.
        locks.mkdir(parents=True, exist_ok=True)
        _lock(stack, locks / (digest(endpoint) + ".lock"))
        gaps = validate_canonical_proof(canonical_proof, source, code_sha)
        approval = validate_approved_gaps(canonical_proof, approval_manifest, approval_receipt)
        binding = {"schema": RECEIPT_SCHEMA, **proof_binding(source, code_sha), "target_endpoint": endpoint,
                   "receipt_path": str(receipt_path), "canonical_proof_checksum": digest(canonical_proof),
                   "approval_manifest_checksum": digest(approval_manifest) if approval_manifest is not None else None,
                   **approval}
        receipt = TransferReceipt(receipt_path, binding)
        stack.callback(receipt.close)
        receipt.mark("incomplete")
        try:
            count = 0
            for name, policy in source.policies.items():
                existing = client.collection_exists(name)
                if existing:
                    params = client.get_collection(name).config.params
                    source.validate_config(params.model_dump(mode="json"), policy)
                else:
                    _require(receipt.db.execute("SELECT 1 FROM verified_points WHERE collection_name=? LIMIT 1", (name,)).fetchone() is None,
                             "Target lost a previously verified collection")
                    params = models.CreateCollection.model_validate(source.configs[name])
                    client.create_collection(name, vectors_config=params.vectors, sparse_vectors_config=params.sparse_vectors)
                # Reject every extra, conflicting or corrupt prefix before any upsert.
                offset, seen, cursors = None, set(), set()
                expected_count = sum(1 for u in canonical_proof["units"] if u[0] == name)
                with _ro(source.manifest) as db:
                    while True:
                        rows, offset = client.scroll(name, limit=BATCH_SIZE, offset=offset, with_payload=True, with_vectors=True)
                        for point in rows:
                            key = str(point.id)
                            _require(key not in seen and db.execute("SELECT 1 FROM candidate_units WHERE collection_name=? AND unit_id=?", (name, key)).fetchone(),
                                     "Target contains duplicate or extra points")
                            seen.add(key)
                        if offset is None:
                            break
                        token = str(offset)
                        _require(rows and token not in cursors and len(seen) <= expected_count,
                                 "Target scroll failed to advance within canonical inventory")
                        cursors.add(token)
                for page in source.pages(name):
                    points = [p for p, _ in page]
                    present = [p for p in points if str(p.id) in seen]
                    if present:
                        _retrieve(client, name, present, policy)
                    for point, checkpoint in page:
                        old = receipt.db.execute("SELECT input_digest,dense_digest,point_digest FROM verified_points WHERE collection_name=? AND unit_id=?", (name, str(point.id))).fetchone()
                        expected = (checkpoint[1], checkpoint[2], point_digest(point))
                        _require(old is None or (old == expected and str(point.id) in seen),
                                 "Verified receipt prefix/input/vector changed or disappeared")
                    pending = [p for p in points if str(p.id) not in seen]
                    if pending:
                        client.upsert(name, pending, wait=True)
                    _retrieve(client, name, points, policy)
                    receipt.db.executemany("INSERT OR REPLACE INTO verified_points VALUES(?,?,?,?,?)",
                        [(name, str(p.id), row[1], row[2], point_digest(p)) for p, row in page])
                    receipt.db.commit()
                    count += len(points)
                # Full second pass prevents subset/extra acceptance after last batch.
                actual_count, offset, cursors, final_ids = 0, None, set(), set()
                while True:
                    rows, offset = client.scroll(name, limit=BATCH_SIZE, offset=offset, with_payload=True, with_vectors=True)
                    for point in rows:
                        row = receipt.db.execute("SELECT 1 FROM verified_points WHERE collection_name=? AND unit_id=?", (name, str(point.id))).fetchone()
                        _require(row is not None and str(point.id) not in final_ids,
                                 "Target contains an unverified extra or repeated point")
                        final_ids.add(str(point.id))
                    actual_count += len(rows)
                    if offset is None:
                        break
                    token = str(offset)
                    _require(rows and token not in cursors and actual_count <= expected_count,
                             "Target scroll repeated or exceeded canonical coverage")
                    cursors.add(token)
                _require(actual_count == expected_count == client.count(name, exact=True).count,
                         "Target count/ID coverage mismatch")
                for page in source.pages(name):
                    _retrieve(client, name, [p for p, _ in page], policy)
            _require(receipt.db.execute("SELECT count(*) FROM verified_points").fetchone()[0] == count,
                     "Receipt contains extra verified units")
            source.assert_unchanged()
            report = {"transfer_complete": True, "full_coverage": not gaps, "candidate_ready": not gaps,
                      "cutover_eligible_with_approved_gaps": bool(gaps), "approved_gap_count": len(gaps),
                      "verified_points": count, "verified_collections": len(source.policies),
                      "binding_digest": digest(binding), "production_cutover": "not_performed",
                      "runtime_metadata": "not_changed", "media_live_ledger": "not_adopted"}
            receipt.mark("verified", report)
            return report
        except BaseException:
            receipt.mark("incomplete")
            raise
