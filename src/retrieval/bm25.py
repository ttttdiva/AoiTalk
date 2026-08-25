"""Small, dependency-free BM25 retrieval core.

The core in this module deliberately knows nothing about users, projects,
filesystem paths, databases, or cache directories.  Callers provide an
already-authorized iterable of :class:`BM25Document` objects.  Scope/auth
adapters live outside this module and may use :class:`IndexIdentity` as an
opaque cache identity.

The implementation is intentionally lexical.  It is a candidate finder for
the existing ``read_file``/``search_files``/RepoMap tools, not a replacement
for those tools and not a semantic/vector index.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import threading
import unicodedata
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Protocol, Sequence


_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
_CAMEL_BOUNDARY_RE = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_SCRIPT_RUN_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[^\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+"
)
_PATH_SEPARATORS_RE = re.compile(r"[/\\.:#@]+")


@dataclass(frozen=True, slots=True)
class DocumentFingerprint:
    """Cheap source freshness metadata supplied by an authorized adapter.

    ``content_hash`` is optional.  When omitted the core computes a SHA-256
    digest from the supplied text, keeping tests and in-memory callers safe
    while allowing filesystem adapters to use mtime/size fast paths.
    """

    mtime_ns: int | None = None
    size: int | None = None
    content_hash: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "mtime_ns": self.mtime_ns,
            "size": self.size,
            "content_hash": self.content_hash,
        }

    def key(self) -> tuple[int | None, int | None, str | None]:
        return (self.mtime_ns, self.size, self.content_hash)


@dataclass(frozen=True, slots=True)
class IndexIdentity:
    """Opaque identity for one authorization/security scope.

    Scope adapters own the meaning and validation of these fields.  The core
    only offers deterministic serialization for a cache key and never treats a
    cache hit as authorization.
    """

    scope_type: str
    canonical_target: str
    visibility_scope: str = ""
    user_id: str | None = None
    project_id: str | None = None
    app_id: str | None = None
    binding_id: str | None = None
    release_id: str | None = None
    artifact_sha256: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "scope_type": self.scope_type,
            "canonical_target": self.canonical_target,
            "visibility_scope": self.visibility_scope,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "app_id": self.app_id,
            "binding_id": self.binding_id,
            "release_id": self.release_id,
            "artifact_sha256": self.artifact_sha256,
        }

    def cache_key(self) -> str:
        """Return a stable, non-reversible cache key for the identity."""

        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class BM25Document:
    """One authorized text document consumed by :class:`BM25Index`."""

    document_id: str
    path: str
    content: str
    filename: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    fingerprint: DocumentFingerprint | None = None

    def __post_init__(self) -> None:
        document_id = str(self.document_id or "").strip()
        if not document_id:
            raise ValueError("document_id is required")
        path = str(self.path or "").replace("\\", "/")
        content = str(self.content or "")
        filename = self.filename
        if filename is None:
            filename = path.rsplit("/", 1)[-1] if path else ""
        filename = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
        metadata = dict(self.metadata or {})
        object.__setattr__(self, "document_id", document_id)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "metadata", MappingProxyType(metadata))

    def effective_fingerprint(self) -> DocumentFingerprint:
        """Return supplied freshness data, filling a missing content hash."""

        supplied = self.fingerprint
        if supplied is not None and supplied.content_hash:
            return supplied
        digest = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        return DocumentFingerprint(
            mtime_ns=supplied.mtime_ns if supplied else None,
            size=supplied.size if supplied and supplied.size is not None else len(self.content.encode("utf-8")),
            content_hash=digest,
        )


@dataclass(frozen=True, slots=True)
class BM25Chunk:
    """Bounded retrieval unit with 1-based inclusive source line numbers."""

    chunk_id: str
    document_id: str
    path: str
    text: str
    start_line: int
    end_line: int
    heading_path: tuple[str, ...] = ()
    token_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "heading_path", tuple(self.heading_path or ()))
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("chunk line range is invalid")


@dataclass(frozen=True, slots=True)
class BM25Hit:
    """Public ranked candidate returned by :class:`BM25Index`."""

    chunk_id: str
    document_id: str
    path: str
    start_line: int
    end_line: int
    score: float
    rank: int
    snippet: str

    def to_dict(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "score": float(self.score),
            "rank": self.rank,
            "snippet": self.snippet,
        }


@dataclass(frozen=True, slots=True)
class RefreshStats:
    """Result of an atomic index refresh."""

    status: str = "updated"
    scanned_documents: int = 0
    indexed_documents: int = 0
    reused_documents: int = 0
    added_documents: int = 0
    updated_documents: int = 0
    removed_documents: int = 0
    unchanged_documents: int = 0
    chunk_count: int = 0
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    # Short aliases keep adapters written against the early proposal
    # (`added`, `updated`, `removed`, `unchanged`, `chunks`) source-compatible
    # while the frozen public fields retain explicit document/chunk wording.
    @property
    def added(self) -> int:
        return self.added_documents

    @property
    def updated(self) -> int:
        return self.updated_documents

    @property
    def removed(self) -> int:
        return self.removed_documents

    @property
    def unchanged(self) -> int:
        return self.unchanged_documents

    @property
    def chunks(self) -> int:
        return self.chunk_count

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "scanned_documents": self.scanned_documents,
            "indexed_documents": self.indexed_documents,
            "reused_documents": self.reused_documents,
            "added_documents": self.added_documents,
            "updated_documents": self.updated_documents,
            "removed_documents": self.removed_documents,
            "unchanged_documents": self.unchanged_documents,
            "chunk_count": self.chunk_count,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class BM25SearchResponse:
    """Bounded, model-facing search envelope."""

    success: bool
    query: str
    scope: str
    results: tuple[BM25Hit, ...] = ()
    total_returned: int = 0
    truncated: bool = False
    total_chars: int = 0
    error: str | None = None

    @property
    def hits(self) -> tuple[BM25Hit, ...]:
        """Compatibility alias for adapters using the early ``hits`` name."""

        return self.results

    @property
    def total(self) -> int:
        """Compatibility alias for the number of returned hits."""

        return self.total_returned

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "query": self.query,
            "scope": self.scope,
            "results": [item.to_dict() for item in self.results],
            "total_returned": self.total_returned,
            "truncated": self.truncated,
            "total_chars": self.total_chars,
            "error": self.error,
        }


class AuthorizedDocumentStream(Protocol):
    """Optional adapter protocol implemented by scope/security owners."""

    def __iter__(self) -> Iterable[BM25Document]:  # pragma: no cover - Protocol
        ...


class BM25Tokenizer:
    """Deterministic identifier/path/CJK tokenizer used by query and docs."""

    @staticmethod
    def normalize(text: str) -> str:
        return unicodedata.normalize("NFKC", str(text or "")).casefold()

    @classmethod
    def _identifier_parts(cls, value: str) -> list[str]:
        normalized = unicodedata.normalize("NFKC", str(value or ""))
        if not normalized:
            return []
        parts: list[str] = []
        # Keep the unsplit identifier/path component so exact identifiers rank
        # above incidental sub-token matches.
        for raw in _PATH_SEPARATORS_RE.split(normalized):
            if not raw:
                continue
            component_parts: list[str] = []
            camel_parts = [part for part in _CAMEL_BOUNDARY_RE.split(raw) if part]
            for camel in camel_parts:
                for segment in re.split(r"[_\-\s]+", camel):
                    if segment:
                        component_parts.append(segment)
            # Include the complete component for exact filename/identifier
            # matching, while still adding its subparts above.
            parts.extend(component_parts)
            if raw not in component_parts:
                parts.append(raw)
        return parts

    @classmethod
    def tokenize(cls, text: str) -> tuple[str, ...]:
        """Tokenize text while preserving useful identifier/path terms.

        ASCII identifiers are emitted as both their complete component and
        camel/snake/kebab parts.  Japanese runs keep the whole run and add
        overlapping bigrams; this is intentionally lexical and does not infer
        translations or semantics.
        """

        normalized = unicodedata.normalize("NFKC", str(text or ""))
        if not normalized:
            return ()
        tokens: list[str] = []
        for component in cls._identifier_parts(normalized):
            # Preserve the complete snake/kebab/camel identifier in addition
            # to its split terms (for example ``get_user_profile`` and
            # ``get``, ``user``, ``profile``).  ``_TOKEN_RE`` intentionally
            # treats underscores as separators, so emit this exact form
            # before extracting ordinary terms.
            normalized_component = component.casefold()
            preserve_identifier = (
                "_" in component
                or "-" in component
                or any(character.isupper() for character in component)
            )
            if preserve_identifier:
                tokens.append(normalized_component)
            for raw_token in _TOKEN_RE.findall(component):
                token = raw_token.casefold()
                if not token:
                    continue
                if not (preserve_identifier and token == normalized_component):
                    tokens.append(token)
                # Keep Latin/number terms searchable when a CJK run and an
                # ASCII identifier touch without punctuation (``認証Auth``).
                script_tokens: set[str] = set()
                for script_run in _SCRIPT_RUN_RE.findall(raw_token):
                    script_token = script_run.casefold()
                    if script_token != token:
                        tokens.append(script_token)
                        script_tokens.add(script_token)
                # CJK runs need a lightweight segmentation fallback.  Keep
                # both the full run and bigrams so short Japanese queries work.
                for run in _CJK_RE.findall(raw_token):
                    if len(run) >= 2:
                        for index in range(len(run) - 1):
                            bigram = run[index : index + 2]
                            if bigram.casefold() in script_tokens or bigram.casefold() == token:
                                continue
                            tokens.append(bigram)
        return tuple(tokens)

    @classmethod
    def fields(cls, document: BM25Document) -> dict[str, tuple[str, ...]]:
        return {
            "body": cls.tokenize(document.content),
            "path": cls.tokenize(document.path),
            "filename": cls.tokenize(document.filename or ""),
        }


class BM25Chunker:
    """Heading-aware line chunker with bounded character windows."""

    def __init__(self, max_chars: int = 1600, max_lines: int = 80, overlap_lines: int = 2) -> None:
        self.max_chars = max(1, int(max_chars))
        self.max_lines = max(1, int(max_lines))
        self.overlap_lines = max(0, min(int(overlap_lines), self.max_lines - 1))

    @staticmethod
    def _chunk_id(document_id: str, start_line: int, end_line: int, text: str) -> str:
        raw = f"{document_id}:{start_line}:{end_line}:{text}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:24]

    def _split_window(
        self,
        document: BM25Document,
        lines: Sequence[str],
        start_index: int,
        end_index: int,
        heading_path: tuple[str, ...],
        tokenizer: BM25Tokenizer,
    ) -> list[BM25Chunk]:
        chunks: list[BM25Chunk] = []
        cursor = start_index
        while cursor < end_index:
            window_start = cursor
            char_count = 0
            line_count = 0
            emitted_long_line = False
            while cursor < end_index and line_count < self.max_lines:
                line = lines[cursor]
                next_count = char_count + len(line) + (1 if line_count else 0)
                if line_count and next_count > self.max_chars:
                    break
                if not line_count and len(line) > self.max_chars:
                    # A single huge line is split into bounded text pieces;
                    # both pieces retain the source line number.
                    for offset in range(0, len(line), self.max_chars):
                        text = line[offset : offset + self.max_chars]
                        if text.strip():
                            chunks.append(
                                BM25Chunk(
                                    chunk_id=self._chunk_id(
                                        document.document_id,
                                        cursor + 1,
                                        cursor + 1,
                                        text,
                                    ),
                                    document_id=document.document_id,
                                    path=document.path,
                                    text=text,
                                    start_line=cursor + 1,
                                    end_line=cursor + 1,
                                    heading_path=heading_path,
                                    token_count=len(tokenizer.tokenize(text)),
                                )
                            )
                    cursor += 1
                    emitted_long_line = True
                    break
                char_count = next_count
                cursor += 1
                line_count += 1
            if cursor == window_start:
                cursor += 1
            # A long source line was already emitted as bounded pieces above;
            # do not append a second, unbounded copy of that line.
            text = "\n".join(lines[window_start:cursor]).strip()
            if text and not emitted_long_line:
                chunks.append(
                    BM25Chunk(
                        chunk_id=self._chunk_id(
                            document.document_id,
                            window_start + 1,
                            cursor,
                            text,
                        ),
                        document_id=document.document_id,
                        path=document.path,
                        text=text,
                        start_line=window_start + 1,
                        end_line=cursor,
                        heading_path=heading_path,
                        token_count=len(tokenizer.tokenize(text)),
                    )
                )
            if cursor >= end_index:
                break
            cursor = max(window_start + 1, cursor - self.overlap_lines)
        return chunks

    def chunk(self, document: BM25Document, tokenizer: BM25Tokenizer | None = None) -> tuple[BM25Chunk, ...]:
        tokenizer = tokenizer or BM25Tokenizer()
        lines = document.content.splitlines()
        if not lines:
            # Keep a path/filename-only posting for empty files.  This lets
            # authorized callers discover an empty ``README.md`` by path
            # without inventing source text; the adapter may re-read it when
            # rendering the final hit.
            return (
                BM25Chunk(
                    chunk_id=self._chunk_id(document.document_id, 1, 1, ""),
                    document_id=document.document_id,
                    path=document.path,
                    text="",
                    start_line=1,
                    end_line=1,
                    heading_path=(),
                    token_count=0,
                ),
            )
        chunks: list[BM25Chunk] = []
        section_start = 0
        heading_path: list[str] = []

        def flush(end_index: int) -> None:
            nonlocal section_start
            if end_index <= section_start:
                return
            chunks.extend(
                self._split_window(
                    document,
                    lines,
                    section_start,
                    end_index,
                    tuple(heading_path),
                    tokenizer,
                )
            )
            section_start = end_index

        for index, line in enumerate(lines):
            match = _HEADING_RE.match(line)
            if match:
                flush(index)
                level = len(match.group(1))
                heading_path[:] = heading_path[: level - 1] + [match.group(2).strip()]
        flush(len(lines))
        return tuple(chunks)


@dataclass(frozen=True, slots=True)
class _ChunkRecord:
    chunk: BM25Chunk
    fingerprint: DocumentFingerprint
    fields: Mapping[str, tuple[str, ...]]
    counts: Mapping[str, Counter[str]]


@dataclass(frozen=True, slots=True)
class _Snapshot:
    records: Mapping[str, _ChunkRecord]
    document_fingerprints: Mapping[str, DocumentFingerprint]
    document_paths: Mapping[str, str]
    postings: Mapping[str, Mapping[str, int]]
    field_postings: Mapping[str, Mapping[str, Mapping[str, int]]]
    document_count: int
    avg_lengths: Mapping[str, float]


def _empty_snapshot() -> _Snapshot:
    return _Snapshot(
        records=MappingProxyType({}),
        document_fingerprints=MappingProxyType({}),
        document_paths=MappingProxyType({}),
        postings=MappingProxyType({}),
        field_postings=MappingProxyType({}),
        document_count=0,
        avg_lengths=MappingProxyType({"body": 1.0, "path": 1.0, "filename": 1.0}),
    )


class BM25Index:
    """In-memory BM25 index with atomic copy-on-write refreshes.

    ``refresh`` accepts only an iterable of already-authorized documents.  It
    never walks a directory, opens a path, consults ``Path.cwd()``, or writes a
    cache file.  Scope/security adapters own those responsibilities.
    """

    DEFAULT_FIELD_WEIGHTS = {"body": 1.0, "path": 1.35, "filename": 1.55}

    def __init__(
        self,
        identity: IndexIdentity | None = None,
        *,
        tokenizer: BM25Tokenizer | None = None,
        chunker: BM25Chunker | None = None,
        k1: float = 1.2,
        b: float = 0.75,
        field_weights: Mapping[str, float] | None = None,
        default_max_results: int = 10,
        default_max_chars: int = 4000,
    ) -> None:
        self.identity = identity
        self.tokenizer = tokenizer or BM25Tokenizer()
        self.chunker = chunker or BM25Chunker()
        self.k1 = max(0.01, float(k1))
        self.b = max(0.0, min(float(b), 1.0))
        weights = dict(self.DEFAULT_FIELD_WEIGHTS)
        if field_weights:
            for field_name, weight in field_weights.items():
                if field_name in weights:
                    weights[field_name] = max(0.0, float(weight))
        self.field_weights = MappingProxyType(weights)
        self.default_max_results = max(1, min(int(default_max_results), 100))
        self.default_max_chars = max(1, int(default_max_chars))
        self._snapshot = _empty_snapshot()
        self._documents: dict[str, BM25Document] = {}
        self._records_by_document: dict[str, tuple[_ChunkRecord, ...]] = {}
        self._refresh_lock = threading.RLock()
        self._built = False

    @property
    def is_built(self) -> bool:
        return self._built

    @property
    def document_count(self) -> int:
        return self._snapshot.document_count

    @property
    def chunk_count(self) -> int:
        return len(self._snapshot.records)

    def _make_records(self, document: BM25Document) -> tuple[_ChunkRecord, ...]:
        fingerprint = document.effective_fingerprint()
        chunks = self.chunker.chunk(document, self.tokenizer)
        records: list[_ChunkRecord] = []
        for chunk in chunks:
            body_tokens = self.tokenizer.tokenize(chunk.text)
            fields = {
                "body": body_tokens,
                "path": self.tokenizer.tokenize(document.path),
                "filename": self.tokenizer.tokenize(document.filename or ""),
            }
            records.append(
                _ChunkRecord(
                    chunk=chunk,
                    fingerprint=fingerprint,
                    fields=MappingProxyType(fields),
                    counts=MappingProxyType(
                        {field_name: Counter(tokens) for field_name, tokens in fields.items()}
                    ),
                )
            )
        return tuple(records)

    @staticmethod
    def _freeze_nested_mapping(value: Mapping[str, Mapping[str, int]]) -> Mapping[str, Mapping[str, int]]:
        return MappingProxyType(
            {
                key: MappingProxyType(dict(inner))
                for key, inner in value.items()
            }
        )

    def _build_snapshot(self, records_by_document: Mapping[str, tuple[_ChunkRecord, ...]]) -> _Snapshot:
        records: dict[str, _ChunkRecord] = {}
        postings: dict[str, dict[str, int]] = {}
        field_postings: dict[str, dict[str, dict[str, int]]] = {
            "body": {},
            "path": {},
            "filename": {},
        }
        lengths: dict[str, Counter[str]] = {}
        document_paths: dict[str, str] = {}
        document_fingerprints: dict[str, DocumentFingerprint] = {}
        for document_id, doc_records in records_by_document.items():
            if doc_records:
                document_paths[document_id] = doc_records[0].chunk.path
                document_fingerprints[document_id] = doc_records[0].fingerprint
            for record in doc_records:
                chunk_id = record.chunk.chunk_id
                records[chunk_id] = record
                lengths[chunk_id] = Counter(
                    {field_name: len(tokens) for field_name, tokens in record.fields.items()}
                )
                for field_name, counts in record.counts.items():
                    field_map = field_postings[field_name]
                    for token, frequency in counts.items():
                        field_map.setdefault(token, {})[chunk_id] = frequency
                        postings.setdefault(token, {})[chunk_id] = max(
                            postings.setdefault(token, {}).get(chunk_id, 0), frequency
                        )
        avg_lengths: dict[str, float] = {}
        for field_name in ("body", "path", "filename"):
            total = sum(lengths.get(chunk_id, {}).get(field_name, 0) for chunk_id in records)
            avg_lengths[field_name] = total / len(records) if records else 1.0
            if avg_lengths[field_name] <= 0:
                avg_lengths[field_name] = 1.0
        return _Snapshot(
            records=MappingProxyType(dict(records)),
            document_fingerprints=MappingProxyType(dict(document_fingerprints)),
            document_paths=MappingProxyType(dict(document_paths)),
            postings=self._freeze_nested_mapping(postings),
            field_postings=MappingProxyType(
                {
                    field_name: self._freeze_nested_mapping(field_map)
                    for field_name, field_map in field_postings.items()
                }
            ),
            document_count=len(records_by_document),
            avg_lengths=MappingProxyType(avg_lengths),
        )

    def refresh(self, documents: Iterable[BM25Document]) -> RefreshStats:
        """Refresh from an authorized stream and atomically publish a snapshot."""

        with self._refresh_lock:
            try:
                incoming: dict[str, BM25Document] = {}
                for document in documents:
                    if not isinstance(document, BM25Document):
                        raise TypeError("documents must contain BM25Document values")
                    if document.document_id in incoming:
                        raise ValueError(f"duplicate document_id: {document.document_id}")
                    incoming[document.document_id] = document

                previous_documents = dict(self._documents)
                previous_records = dict(self._records_by_document)
                next_records: dict[str, tuple[_ChunkRecord, ...]] = {}
                added = updated = reused = unchanged = 0
                for document_id, document in incoming.items():
                    old = previous_documents.get(document_id)
                    fingerprint = document.effective_fingerprint()
                    old_fp = old.effective_fingerprint() if old else None
                    same_metadata = (
                        old is not None
                        and old.path == document.path
                        and old.filename == document.filename
                        and old_fp is not None
                        and old_fp.key() == fingerprint.key()
                    )
                    if same_metadata and document_id in previous_records:
                        next_records[document_id] = previous_records[document_id]
                        reused += 1
                        unchanged += 1
                    else:
                        next_records[document_id] = self._make_records(document)
                        if old is None:
                            added += 1
                        else:
                            updated += 1

                removed = len(set(previous_documents) - set(incoming))
                snapshot = self._build_snapshot(next_records)
                # Swap all derived state together.  Readers always observe the
                # previous or next complete snapshot, never a partial publish.
                self._documents = incoming
                self._records_by_document = next_records
                self._snapshot = snapshot
                self._built = True
                return RefreshStats(
                    status="updated",
                    scanned_documents=len(incoming),
                    indexed_documents=len(next_records),
                    reused_documents=reused,
                    added_documents=added,
                    updated_documents=updated,
                    removed_documents=removed,
                    unchanged_documents=unchanged,
                    chunk_count=len(snapshot.records),
                )
            except Exception as exc:
                # Do not mutate state until snapshot construction succeeds.
                return RefreshStats(status="failed", error=str(exc), chunk_count=self.chunk_count)

    def build(self, documents: Iterable[BM25Document]) -> RefreshStats:
        """Explicit first build alias for adapters that prefer that wording."""

        return self.refresh(documents)

    def upsert(self, document: BM25Document) -> RefreshStats:
        with self._refresh_lock:
            documents = dict(self._documents)
            documents[document.document_id] = document
            return self.refresh(documents.values())

    def remove(self, document_id: str) -> RefreshStats:
        with self._refresh_lock:
            documents = dict(self._documents)
            documents.pop(str(document_id), None)
            return self.refresh(documents.values())

    def _score_record(self, record: _ChunkRecord, query_terms: Counter[str], snapshot: _Snapshot) -> float:
        score = 0.0
        chunk_id = record.chunk.chunk_id
        total_chunks = max(1, len(snapshot.records))
        for field_name, weight in self.field_weights.items():
            if weight <= 0:
                continue
            counts = record.counts[field_name]
            length = len(record.fields[field_name])
            avg_length = snapshot.avg_lengths.get(field_name, 1.0)
            field_score = 0.0
            field_map = snapshot.field_postings[field_name]
            for token, query_frequency in query_terms.items():
                term_postings = field_map.get(token)
                if not term_postings:
                    continue
                tf = counts.get(token, 0)
                if not tf:
                    continue
                df = len(term_postings)
                idf = math.log1p((total_chunks - df + 0.5) / (df + 0.5))
                denominator = tf + self.k1 * (1.0 - self.b + self.b * length / avg_length)
                field_score += idf * ((tf * (self.k1 + 1.0)) / denominator) * query_frequency
            score += field_score * weight
        return score

    @staticmethod
    def _snippet(text: str, query: str, limit: int) -> str:
        limit = max(1, int(limit))
        clean = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(clean) <= limit:
            return clean
        if limit == 1:
            return "…"
        normalized_query = BM25Tokenizer.normalize(query).strip()
        haystack = BM25Tokenizer.normalize(clean)
        position = haystack.find(normalized_query) if normalized_query else -1
        if position < 0:
            for token in BM25Tokenizer.tokenize(query):
                position = haystack.find(token)
                if position >= 0:
                    break
        if position < 0:
            return clean[: max(1, limit - 1)].rstrip() + "…"
        radius = max(1, (limit - 1) // 2)
        start = max(0, position - radius)
        end = min(len(clean), start + limit - 1)
        if end - start < limit - 1:
            start = max(0, end - (limit - 1))
        snippet = clean[start:end].strip()
        if start > 0:
            snippet = "…" + snippet
        if end < len(clean):
            snippet = snippet.rstrip() + "…"
        return snippet[:limit]

    def search(
        self,
        query: str,
        *,
        max_results: int | None = None,
        max_chars: int | None = None,
        snippet_chars: int = 280,
        path_prefix: str | None = None,
        scope: str | None = None,
    ) -> BM25SearchResponse:
        """Return deterministic ranked, bounded candidates for ``query``."""

        query_text = str(query or "").strip()
        response_scope = str(scope) if scope else (self.identity.scope_type if self.identity else "")
        if not query_text:
            return BM25SearchResponse(True, query_text, response_scope)
        snapshot = self._snapshot
        query_terms = Counter(self.tokenizer.tokenize(query_text))
        if not query_terms or not snapshot.records:
            return BM25SearchResponse(True, query_text, response_scope)
        prefix = str(path_prefix).replace("\\", "/").casefold() if path_prefix else None
        scored: list[tuple[float, BM25Chunk, _ChunkRecord]] = []
        for record in snapshot.records.values():
            if prefix:
                candidate_path = record.chunk.path.casefold()
                normalized_prefix = prefix.rstrip("/")
                if candidate_path != normalized_prefix and not candidate_path.startswith(
                    normalized_prefix + "/"
                ):
                    continue
            score = self._score_record(record, query_terms, snapshot)
            if score > 0:
                scored.append((score, record.chunk, record))
        scored.sort(key=lambda item: (-item[0], item[1].path.casefold(), item[1].start_line, item[1].chunk_id))
        result_limit = self.default_max_results if max_results is None else max(1, min(int(max_results), 100))
        char_budget = self.default_max_chars if max_chars is None else max(1, int(max_chars))
        selected = scored[:result_limit]
        truncated = len(scored) > len(selected)
        hits: list[BM25Hit] = []
        total_chars = 0
        for rank, (score, chunk, _record) in enumerate(selected, start=1):
            remaining = char_budget - total_chars
            if remaining <= 0:
                truncated = True
                break
            snippet_limit = min(max(1, int(snippet_chars)), remaining)
            normalized_chunk_text = re.sub(r"\s+", " ", chunk.text).strip()
            if len(normalized_chunk_text) > snippet_limit:
                truncated = True
            snippet = self._snippet(chunk.text or chunk.path, query_text, snippet_limit)
            if not snippet:
                continue
            if len(snippet) > remaining:
                snippet = snippet[:remaining]
                truncated = True
            total_chars += len(snippet)
            hits.append(
                BM25Hit(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    path=chunk.path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    score=float(score),
                    rank=rank,
                    snippet=snippet,
                )
            )
        return BM25SearchResponse(
            success=True,
            query=query_text,
            scope=response_scope,
            results=tuple(hits),
            total_returned=len(hits),
            truncated=truncated,
            total_chars=total_chars,
        )


__all__ = [
    "BM25Document",
    "DocumentFingerprint",
    "BM25Chunk",
    "BM25Hit",
    "RefreshStats",
    "IndexIdentity",
    "BM25Index",
    "BM25SearchResponse",
]
