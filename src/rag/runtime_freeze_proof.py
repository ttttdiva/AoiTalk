"""Fresh canonical evidence for the externally stopped-writer transfer gate.

Reuses canonical candidate enumerators and current reader policy, with no
encoder/Qdrant/ledger constructor or source commit. Temporary media preparation
is isolated and cleaned; originals, runtime indexes and shared caches are read-only.
Two fresh SQL transactions detect drift; they do not acquire a distributed freeze.
"""

from __future__ import annotations
from datetime import date, datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid

import numpy as np
from qdrant_client.local.persistence import CollectionPersistence
from sqlalchemy import select

from ..memory import models as dbm
from ..security.field_crypto import (
    _readonly_data_key_access,
    decrypt_text_if_needed,
    decrypt_json_value_if_needed,
)
from ..services.storage_roots import (
    StorageRoot,
    assert_no_links,
    lexical_path,
    read_registry,
)
from ..services.media_asset_storage import open_asset, remove_asset
from .docs_index import DocsIndexService
from ..knowledge.index_service import HashingSparseEncoder, KnowledgeIndexService
from .embedding_identity import DIMENSION, embedding_collection_name
from .media import MediaPreparationService
from .media_index import MediaIndexService
from .runtime_candidate import RuntimeCandidate, _input_digest
from .shadow_migration import MAX_SHADOW_RECORDS, read_only_source_session
from . import runtime_transfer as rt

REGISTRATION_SCHEMA = "aoitalk-canonical-registration-v1"
# Explicit scalar projections exclude source bodies, credentials and live ACKs.
REGISTRATION_COLUMNS = {
    "docs_library": (
        dbm.DocsLibrary,
        "id name owner_user_id library_type settings_json updated_at",
    ),
    "knowledge_source": (
        dbm.KnowledgeSource,
        "id name root_path owner_user_id source_type access_policy include_patterns exclude_patterns status updated_at",
    ),
    "conversation_message": (
        dbm.ConversationMessage,
        "id session_id role message_metadata deleted_at is_active_branch parent_message_id updated_at",
    ),
    "context_memory": (
        dbm.ContextMemory,
        "id user_id scope_type scope_id project_id task_id session_id version status sensitivity expires_at updated_at supersedes_id",
    ),
    "chat_attachment": (
        dbm.ConversationMessage,
        "id session_id role message_metadata deleted_at is_active_branch parent_message_id updated_at",
    ),
    "task_attachment": (
        dbm.TaskAttachment,
        "id task_id project_id file_path display_name mime_type size_bytes kind created_by attachment_metadata",
    ),
    "docs_attachment": (
        dbm.KnowledgeAttachment,
        "id node_id file_name file_path mime_type size_bytes created_by attachment_metadata",
    ),
    "knowledge_document": (
        dbm.KnowledgeDocument,
        "id source_id path resolved_absolute_path title extension mime_type content_hash size_bytes status project_refs task_refs frontmatter_json updated_at",
    ),
    "generated_media": (
        dbm.GeneratedMedia,
        "id owner_user_id context_type context_id bind_type bind_id storage_key relative_path mime_type byte_size status prompt_meta updated_at",
    ),
    "generation_output": (
        dbm.GenerationOutput,
        "id owner_user_id project_id external_asset_id external_output_version byte_binding_state sha256 mime_type provenance_hash",
    ),
    "meeting_audio": (
        dbm.MeetingProcessingJob,
        "id actor_user_id request_sha256 _request_json audio_upload_id audio_file_name audio_mime_type audio_size_bytes audio_sha256 status updated_at",
    ),
}
# Every source is conservatively bound to the current global authority graph:
# even unrelated ACL edits invalidate a previous exact gap manifest.
AUTHORITY_COLUMNS = (
    (dbm.User, "id role is_active"),
    (
        dbm.Project,
        "id owner_id space_id knowledge_node_id deleted_at is_completed slug project_metadata",
    ),
    (dbm.ProjectMember, "id project_id user_id permissions"),
    (dbm.Space, "id owner_id slug"),
    (dbm.DocsLibrary, "id owner_user_id library_type"),
    (
        dbm.KnowledgeNode,
        "id docs_library_id project_id parent_id root_page_id archived_at node_type system_key title is_explicit_blank",
    ),
    (dbm.KnowledgeNodeShare, "id node_id user_id permission"),
    (dbm.KnowledgeSupertag, "id docs_library_id system_key"),
    (dbm.KnowledgeSource, "id owner_user_id source_type root_path access_policy"),
    (dbm.KnowledgeSourcePermission, "id source_id user_id project_id permission"),
    (dbm.ConversationSession, "id user_id project_id character_name title deleted_at"),
    (
        dbm.ConversationParticipant,
        "id session_id participant_type participant_id status",
    ),
    (dbm.Task, "id project_id deleted_at"),
    (
        dbm.MediaAssetRevision,
        "id generation_output_id owner_user_id project_id persona_id relative_path sha256 mime_type size_bytes revision_hash",
    ),
)


def _json_value(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=timezone.utc).isoformat()
            if value.tzinfo is None
            else value.astimezone(timezone.utc).isoformat()
        )
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        rt._require(
            all(isinstance(k, str) for k in value),
            "Registration metadata has non-string keys",
        )
        return {k: _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise rt.TransferError("Registration metadata has unsupported values")


async def registration_authority_digest(session):
    """Bounded fresh scalar SQL, never an ORM identity-map snapshot."""
    result = hashlib.sha256(REGISTRATION_SCHEMA.encode())
    for model, fields in (
        *AUTHORITY_COLUMNS,
        (dbm.KnowledgeNodeSupertag, "node_id supertag_id"),
    ):
        names, cursor, count = fields.split(), None, 0
        columns = [getattr(model, n) for n in names]
        keys = (
            [model.id] if hasattr(model, "id") else [model.node_id, model.supertag_id]
        )
        result.update(json.dumps([model.__tablename__, names]).encode())
        while True:
            statement = select(*columns).order_by(*keys).limit(1000)
            if cursor is not None:
                from sqlalchemy import tuple_

                statement = statement.where(tuple_(*keys) > tuple(cursor))
            rows = (await session.execute(statement)).all()
            if not rows:
                break
            count += len(rows)
            rt._require(
                count <= MAX_SHADOW_RECORDS,
                "Canonical authority inventory exceeds bound",
            )
            for row in rows:
                result.update((rt.digest(_json_value(list(row))) + "\n").encode())
            cursor = [getattr(rows[-1], k.key) for k in keys]
    return result.hexdigest()


async def registration_metadata_hash(session, ref, *, authority_digest=None):
    """SQL registration hash, including tombstones; no invented original hash."""
    rt._require(ref.kind in REGISTRATION_COLUMNS, "Unknown canonical source kind")
    model, fields = REGISTRATION_COLUMNS[ref.kind]
    names = fields.split()
    identifier = uuid.UUID(ref.id.split(":", 1)[0])
    row = (
        await session.execute(
            select(*(getattr(model, n) for n in names)).where(model.id == identifier)
        )
    ).first()
    registration = dict(zip(names, row)) if row is not None else None
    if registration is not None and ref.kind == "chat_attachment":
        index = int(ref.id.split(":", 1)[1])
        items = (registration["message_metadata"] or {}).get("attachments") or []
        registration["attachment_index"] = index
        registration["attachment"] = items[index] if 0 <= index < len(items) else None
    if registration is not None and ref.kind == "meeting_audio":
        registration["request_json"] = decrypt_json_value_if_needed(
            registration.pop("_request_json"),
            aad="meeting_processing_jobs.request_json",
        )
    authority = authority_digest or await registration_authority_digest(session)
    rt._require(rt._hash(authority), "Canonical authority digest invalid")
    return rt.digest(
        {
            "schema": REGISTRATION_SCHEMA,
            "source_key": ref.key,
            "registration": _json_value(registration),
            "authority_digest": authority,
        }
    )


async def _protected_roots(session, config):
    roots = {
        lexical_path("data"),
        lexical_path("workspaces"),
        lexical_path(os.environ.get("AOITALK_WORKSPACES_DIR") or "./workspaces"),
    }
    if config.qdrant.local_path:
        roots.add(lexical_path(config.qdrant.local_path))
    roots.update(root.path for root in read_registry().roots)
    cursor = None
    while True:
        stmt = (
            select(dbm.KnowledgeSource.id, dbm.KnowledgeSource.root_path)
            .where(dbm.KnowledgeSource.source_type != "growi")
            .order_by(dbm.KnowledgeSource.id)
            .limit(1000)
        )
        if cursor is not None:
            stmt = stmt.where(dbm.KnowledgeSource.id > cursor)
        rows = (await session.execute(stmt)).all()
        if not rows:
            break
        roots.update(lexical_path(r.root_path) for r in rows)
        cursor = rows[-1].id
    # A broken/linked registration can still be a gap. Protect both names;
    # evidence must not populate its resolved original storage by accident.
    return roots | {root.resolve() for root in roots}


def _validate_scratch_root(source, scratch, protected_roots):
    rt._require(
        scratch == scratch.absolute() == scratch.resolve(),
        "Canonical scratch root is linked or noncanonical",
    )
    assert_no_links(scratch, allow_missing=True)
    protected = [source.target, source.manifest, *protected_roots]
    for provider in json.loads(source.binding[7]):
        protected.extend([Path(provider[1]), Path(provider[5])])
    # A shared .local parent may contain sibling candidate stores. It may
    # never itself be an input store, or lie inside one. The random child is
    # additionally checked with the transfer's symmetric separation rule.
    for path in protected:
        rt._require(
            scratch != path and path not in scratch.parents,
            "Canonical scratch overlaps protected source storage",
        )


class CanonicalProof(dict):
    """Private path guards live only in memory, never in the evidence JSON."""

    def __init__(self, value, roots):
        super().__init__(value)
        self.protected_roots = frozenset(roots)


class _ReadLedger:
    def __init__(self, manifest):
        self.manifest, self.states = manifest, {}

    def connect(self):
        return rt._ro(self.manifest)

    def mark(self, ref, status, *, completed=0, total=None, error=None, **_):
        self.states[ref.key] = (ref, status, error, completed, total)
        rt._require(
            len(self.states) <= MAX_SHADOW_RECORDS,
            "Canonical source inventory exceeds bound",
        )


class _DocsInputs(DocsIndexService):
    def __init__(self, config):
        self.config = config
        self.collection_name = embedding_collection_name(config.docs_collection_name)
        self.sparse_encoder = HashingSparseEncoder()
        self.embedding = SimpleNamespace(dimension=DIMENSION, config=config.embedding)


class _KnowledgeInputs(KnowledgeIndexService):
    def __init__(self, config):
        self.config = config
        self.collection_name = embedding_collection_name(config.qdrant.collection_name)
        self.sparse_encoder = HashingSparseEncoder()
        self.embedding = SimpleNamespace(dimension=DIMENSION, config=config.embedding)


class _MediaInputs(MediaIndexService):
    def __init__(self, config, scratch):
        self.config = config
        self.collection_name = embedding_collection_name("aoitalk_media")
        self.sparse = HashingSparseEncoder()
        self.preparation = MediaPreparationService(cache_dir=scratch)


def _payload(payload):
    return {
        k: v
        for k, v in payload.items()
        if k not in {"text", "lexical_text", "source_file"}
    }


def _check_physical_unit(source, collection, ref, unit, db):
    raw = db.execute(
        "SELECT point FROM points WHERE id=?",
        (CollectionPersistence.encode_key(unit.point.id),),
    ).fetchone()
    rt._require(raw is not None, "Canonical physical unit missing")
    previous = rt._PointUnpickler(io.BytesIO(raw[0])).load()
    rt.validate_point(previous, source.policies[collection])
    rt._require(
        str(previous.id) == str(unit.point.id)
        and rt._source_key(previous, source.policies[collection]) == ref.key
        and rt.digest(_payload(previous.payload))
        == rt.digest(_payload(unit.point.payload)),
        "Physical payload differs from fresh canonical input",
    )
    if isinstance(unit.point.vector, dict):

        def sparse(value):
            return {
                i: float(np.float32(v)) for i, v in zip(value.indices, value.values)
            }

        rt._require(
            sparse(previous.vector["sparse"]) == sparse(unit.point.vector["sparse"]),
            "Physical sparse vector differs from canonical input",
        )
    domain = source.policies[collection].domain
    if domain in {"media", "memory", "context_memory"}:
        key, aad = (
            ("lexical_text", f"media-index:{unit.point.id}")
            if domain == "media"
            else ("text", f"qdrant.payload.text:{ref.id}")
        )
        rt._require(
            decrypt_text_if_needed(previous.payload.get(key), aad=aad) == ("" if domain == "context_memory" else unit.text),
            "Physical text differs from canonical source",
        )


def _check_physical_batch(source, collection, ref, units):
    """Keep each bounded batch on one worker and one read-only connection."""
    path = source.target / "collection" / collection / "storage.sqlite"
    with rt._ro(path) as db:
        result = []
        for unit in units:
            _check_physical_unit(source, collection, ref, unit, db)
            result.append(_input_digest(unit))
        return result


class CanonicalSnapshot(RuntimeCandidate):
    """Canonical builders retain their reader policy; only sinks are replaced."""

    def __init__(self, config, source, *, protected_roots):
        self.config, self.candidate = config, source
        self.protected_roots = protected_roots
        self.checkpoint = SimpleNamespace(ledger=_ReadLedger(source.manifest))
        self.batch_size = max(
            1, min(config.indexing.batch_size, config.embedding.batch_size, 32)
        )
        self.units, self.source_units = {}, {}
        self.scratch = None

    async def io(self, function, *args, **kwargs):
        def read():
            with _readonly_data_key_access():
                return function(*args, **kwargs)

        return await super().io(read)

    async def _docs_service(self):
        return _DocsInputs(self.config)

    async def _knowledge_service(self):
        return _KnowledgeInputs(self.config)

    async def _media_service(self):
        rt._require(self.scratch is not None, "Canonical media scratch not initialized")
        return _MediaInputs(self.config, self.scratch)

    async def _memory_collection(self):
        return embedding_collection_name("aoitalk_conversation_memory")

    async def _context_memory_collection(self):
        from ..memory.context_memory_index import COLLECTION_NAME
        return COLLECTION_NAME

    async def _docs_library_ids(self, session, service):
        ids = set((await session.execute(select(dbm.DocsLibrary.id))).scalars())
        with self.checkpoint.ledger.connect() as db:
            ids.update(
                uuid.UUID(row[0])
                for row in db.execute(
                    "SELECT source_id FROM embedding_sources WHERE kind='docs_library'"
                )
            )
        return sorted(ids)

    def _media_lexical_text(self, text, unit_id):
        return text

    async def batch(self, collection, ref, units, recheck):
        rt._require(
            collection in self.candidate.policies,
            "Canonical collection differs from candidate",
        )
        await recheck()
        keys = [(collection, str(unit.point.id)) for unit in units]
        rt._require(
            len(set(keys)) == len(keys) and not any(key in self.units for key in keys),
            "Duplicate canonical unit",
        )
        digests = await self.io(
            _check_physical_batch, self.candidate, collection, ref, units
        )
        for key, input_digest in zip(keys, digests):
            self.units[key] = [collection, ref.key, key[1], input_digest]
            self.source_units.setdefault((collection, ref.key), set()).add(key[1])
            rt._require(
                len(self.units) <= MAX_SHADOW_RECORDS,
                "Canonical unit inventory exceeds bound",
            )
        await recheck()

    async def finish(self, collection, ref, retained):
        rt._require(
            self.source_units.get((collection, ref.key), set()) == retained,
            "Canonical completion unit set differs",
        )
        self.checkpoint.ledger.mark(
            ref, "ready", completed=len(retained), total=len(retained)
        )

    async def run(self, session, domain="all"):
        rt._require(domain == "all", "Canonical proof requires all domains")
        authority = await registration_authority_digest(session)
        # The same production media preparation code gets a fresh cache each
        # pass, so no old derivative can impersonate current canonical bytes.
        scratch = Path(__file__).resolve().parents[2] / ".local"
        _validate_scratch_root(self.candidate, scratch, self.protected_roots)
        scratch.mkdir(exist_ok=True)
        _validate_scratch_root(self.candidate, scratch, self.protected_roots)
        with tempfile.TemporaryDirectory(
            prefix="canonical-proof-", dir=scratch
        ) as temporary:
            self.scratch = Path(temporary)
            rt.validate_output_paths(self.candidate, [self.scratch])
            for root in self.protected_roots:
                rt._separate(self.scratch, root)
            domains = ["docs", "knowledge", "memory", "media"]
            if self.candidate.binding[0] == "shared_candidate_v2":
                domains.append("context_memory")
            for selected in domains:
                await getattr(self, selected)(session)
        self.scratch = None
        rows = []
        for key, (ref, status, reason, completed, total) in sorted(
            self.checkpoint.ledger.states.items()
        ):
            rt._require(
                status in {"ready", "excluded", "failed"}, "Canonical source unfinished"
            )
            if status == "failed":
                rt._require(
                    reason in rt.ALLOWED_GAPS,
                    "Canonical source failure cannot be approved",
                )
            rows.append(
                {
                    "source_key": key,
                    "status": status,
                    "reason": reason,
                    "registration_metadata_hash": await registration_metadata_hash(
                        session, ref, authority_digest=authority
                    ),
                }
            )
        rt._require(
            await registration_authority_digest(session) == authority,
            "Canonical authority changed during verification",
        )
        return {"sources": rows, "units": [self.units[k] for k in sorted(self.units)]}


async def build_canonical_proof(
    source,
    *,
    session_factory,
    config_reader,
    observed_source_binding,
    code_sha,
    freeze_id,
    confirm_all_writers_stopped=False,
):
    rt._require(
        confirm_all_writers_stopped is True
        and isinstance(freeze_id, str)
        and bool(freeze_id.strip()),
        "Explicit external writer freeze evidence is required",
    )
    rt._require(
        observed_source_binding == source.binding[2],
        "Observed source database binding differs",
    )
    first, original_roots = None, None
    for _ in range(2):
        with _readonly_data_key_access():
            async with read_only_source_session(session_factory) as session:
                config = await config_reader(session)
                rt._require(
                    rt.collection_policies(
                        config.docs_collection_name, config.qdrant.collection_name,
                        format_version=rt.candidate_format_version(source.manifest)
                    )
                    == source.policies,
                    "Current canonical collection configuration differs",
                )
                roots = await _protected_roots(session, config)
                current = await CanonicalSnapshot(
                    config, source, protected_roots=roots
                ).run(session)
        source.assert_unchanged()
        if first is None:
            first, original_roots = current, roots
        else:
            rt._require(
                current == first and roots == original_roots,
                "Canonical metadata/status/unit inventory changed between fresh transactions",
            )
    proof = CanonicalProof(
        {
            "schema": rt.PROOF_SCHEMA,
            "binding": rt.proof_binding(source, code_sha),
            "all_writers_stopped": True,
            "canonical_rechecked": True,
            "deleted_units_reconciled": True,
            "freeze_id": freeze_id,
            **first,
        },
        original_roots,
    )
    rt.validate_canonical_proof(proof, source, code_sha)
    return proof


def write_private_proof(source, output, proof):
    """Exclusive private output through existing descriptor-pinned storage I/O."""
    rt._require(
        isinstance(proof, CanonicalProof),
        "Output requires freshly verified canonical path guards",
    )
    output = Path(output).absolute()
    rt.validate_output_paths(source, [output])
    for root in proof.protected_roots:
        rt._separate(output, root)
    raw = json.dumps(
        proof,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    rt._require(
        len(raw) <= rt.MAX_PROOF_BYTES, "Canonical proof exceeds evidence size bound"
    )
    source.assert_unchanged()
    parent = StorageRoot(
        id="canonical-proof",
        name="Private canonical evidence",
        root_path=str(output.parent),
        external=False,
    )
    owned = None
    try:
        with open_asset(
            parent, output.name, flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL
        ) as (stream, info):
            owned = (info.st_dev, info.st_ino)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            source.assert_unchanged()
    except BaseException:
        if owned is not None:
            remove_asset(parent, output.name, expected_inode=owned)
        raise
