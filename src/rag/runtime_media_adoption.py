"""Frozen operator reconciliation of media into its ordinary live ledger.

No candidate READY copy, encoder, Qdrant write, queue reset, or source DB write.
Other domains' metadata stay untouched. Evidence remains private.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from .media import MediaPreparationError, MediaPreparationService
from .media_index import CanonicalMediaExclusion, MediaIndexService
from .runtime_candidate import CandidateCoverageError, RuntimeCandidate
from . import runtime_transfer as rt
from ..services.search_sources import SourceRef, SourceUnavailable
from ..security.field_crypto import _readonly_data_key_access

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / "data/search/embeddinggemma2.sqlite"
MEDIA_KINDS = frozenset({"generation_output", "generated_media", "task_attachment",
                         "docs_attachment", "knowledge_document", "meeting_audio", "chat_attachment"})


class AllReaderResolver:
    """Union of current legal readers, including nonowners and invalid owners."""
    def __init__(self, session, evidence, registration_hash):
        self.session, self.evidence = session, evidence
        self.registration_hash = registration_hash
        self.resources = []

    async def privacy_masked(self, ref):
        # media_reader performs global privacy exclusion before path resolution.
        # The ordinary sync_source destructive cleanup is intentionally bypassed.
        return False

    async def resolve(self, ref):
        current_hash = await self.registration_hash(self.session, ref)
        if current_hash != self.evidence["registration_metadata_hash"]:
            raise MediaPreparationError("canonical_registration_changed", "Canonical registration changed")
        try:
            with _readonly_data_key_access():
                _, source, exclusion = await RuntimeCandidate.media_reader(self.session, ref)
        except CandidateCoverageError as exc:
            if await self.registration_hash(self.session, ref) != current_hash:
                raise MediaPreparationError("canonical_registration_changed", "Canonical registration changed") from exc
            raise MediaPreparationError(exc.reason, "All canonical readers failed original resolution") from exc
        except SourceUnavailable as exc:
            raise MediaPreparationError("canonical_resolution_failure", "Canonical authority resolution failed") from exc
        if source is None:
            if await self.registration_hash(self.session, ref) != current_hash:
                raise MediaPreparationError("canonical_registration_changed", "Canonical registration changed")
            raise CanonicalMediaExclusion(exclusion, "Canonical source has no current indexable original")
        if source._resource is not None:
            self.resources.append(source._resource)
        return source

    def close(self):
        for resource in reversed(self.resources):
            resource.cleanup()
        self.resources.clear()


def verify_transferred(source, client, *, target_endpoint, receipt_path, canonical_proof,
                       code_sha, approval_manifest=None, approval_receipt=None):
    """Revalidate a completed transfer without repairing its receipt or target."""
    endpoint = rt.endpoint_identity(target_endpoint)
    options = getattr(client, "init_options", {})
    rt._require(options.get("url") and rt.endpoint_identity(options["url"]) == endpoint,
                "Client target differs from the explicit server endpoint")
    gaps = rt.validate_canonical_proof(canonical_proof, source, code_sha)
    approval = rt.validate_approved_gaps(canonical_proof, approval_manifest, approval_receipt)
    receipt_path = Path(receipt_path).absolute()
    binding = {"schema": rt.RECEIPT_SCHEMA, **rt.proof_binding(source, code_sha),
               "target_endpoint": endpoint, "receipt_path": str(receipt_path),
               "canonical_proof_checksum": rt.digest(canonical_proof),
               "approval_manifest_checksum": rt.digest(approval_manifest) if approval_manifest is not None else None,
               **approval}
    expected_count = len(canonical_proof["units"])
    expected_report = {"transfer_complete": True, "full_coverage": not gaps, "candidate_ready": not gaps,
                       "cutover_eligible_with_approved_gaps": bool(gaps), "approved_gap_count": len(gaps),
                       "verified_points": expected_count, "verified_collections": len(source.policies),
                       "binding_digest": rt.digest(binding), "production_cutover": "not_performed",
                       "runtime_metadata": "not_changed", "media_live_ledger": "not_adopted"}
    with rt._ro(receipt_path) as db:
        row = db.execute("SELECT binding,state,report FROM transfer WHERE singleton=1").fetchone()
        rt._require(row is not None and json.loads(row[0]) == binding and row[1] == "verified"
                    and json.loads(row[2]) == expected_report, "Verified transfer receipt/evidence binding mismatch")
        rt._require(db.execute("SELECT count(*) FROM verified_points").fetchone()[0] == expected_count,
                    "Transfer receipt point coverage mismatch")
        count = 0
        for name, policy in source.policies.items():
            rt._require(client.collection_exists(name), "Verified target collection missing")
            source.validate_config(client.get_collection(name).config.params.model_dump(mode="json"), policy)
            expected_ids = set()
            for page in source.pages(name):
                rt._retrieve(client, name, [p for p, _ in page], policy)
                for point, checkpoint in page:
                    saved = db.execute("SELECT input_digest,dense_digest,point_digest FROM verified_points "
                                       "WHERE collection_name=? AND unit_id=?", (name, str(point.id))).fetchone()
                    rt._require(saved == (checkpoint[1], checkpoint[2], rt.point_digest(point)),
                                "Transfer receipt point/input/vector changed")
                    expected_ids.add(str(point.id))
                count += len(page)
            offset, seen, cursors = None, set(), set()
            while True:
                rows, offset = client.scroll(name, limit=rt.BATCH_SIZE, offset=offset,
                                             with_payload=False, with_vectors=False)
                ids = [str(p.id) for p in rows]
                rt._require(len(set(ids)) == len(ids) and not seen.intersection(ids)
                            and set(ids).issubset(expected_ids), "Verified target has extra or repeated points")
                seen.update(ids)
                if offset is None:
                    break
                token = str(offset)
                rt._require(rows and token not in cursors and len(seen) <= len(expected_ids),
                            "Verified target scroll did not advance")
                cursors.add(token)
            rt._require(seen == expected_ids and client.count(name, exact=True).count == len(expected_ids),
                        "Verified target point coverage mismatch")
        rt._require(count == expected_count, "Verified candidate point coverage changed")
    source.assert_unchanged()
    return binding


async def adopt_media(source, client, session, config, *, target_endpoint, receipt_path,
                      canonical_proof, code_sha, registration_hash,
                      approval_manifest=None, approval_receipt=None, ledger_path=DEFAULT_LEDGER,
                      confirm_all_writers_stopped=False, evidence_paths=()):
    """Only ordinary live media ledger mutates, via canonical sync_source.

    CLI pins ledger_path to the runtime default; fixtures inject isolated paths.
    All writers stay frozen. Failure can leave partial ledger progress and never
    grants activation. Reruns fully reconcile rather than copying READY rows.
    """
    rt._require(confirm_all_writers_stopped is True, "Every canonical/index writer must remain stopped")
    ledger_path, receipt_path = Path(ledger_path).absolute(), Path(receipt_path).absolute()
    rt._require(ledger_path.is_file(), "Existing default media live ledger is required")
    locks = ROOT / "data/search/transfer-locks"
    endpoint_lock = locks / (rt.digest(rt.endpoint_identity(target_endpoint)) + ".lock")
    outputs = [ledger_path, Path(str(ledger_path) + ".adoption.lock"), locks, endpoint_lock]
    outputs += [Path(str(ledger_path) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    inputs = [*evidence_paths, receipt_path, Path(str(receipt_path) + ".lock")]
    rt.validate_output_paths(source, outputs, inputs)
    with ExitStack() as stack:
        locks.mkdir(parents=True, exist_ok=True)
        rt._lock(stack, endpoint_lock)
        rt._lock(stack, Path(str(receipt_path) + ".lock"), existing=True)
        rt._lock(stack, Path(str(ledger_path) + ".adoption.lock"))
        verification = dict(target_endpoint=target_endpoint, receipt_path=receipt_path,
                            canonical_proof=canonical_proof, code_sha=code_sha,
                            approval_manifest=approval_manifest, approval_receipt=approval_receipt)
        binding = verify_transferred(source, client, **verification)
        sources = {v["source_key"]: v for v in canonical_proof["sources"]
                   if v["source_key"].partition(":")[0] in MEDIA_KINDS}
        with rt._ro(ledger_path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            rt._require("embedding_sources" in tables and not {"candidate_generation", "shadow_generation"}.intersection(tables),
                        "Live ledger is not an ordinary media ledger")
            rows = db.execute("SELECT source_key,kind,source_id FROM embedding_sources").fetchall()
            rt._require(all(key == f"{kind}:{identifier}" for key, kind, identifier in rows),
                        "Live ledger source locator is inconsistent")
            rt._require(all(key in sources for key, kind, _ in rows
                            if kind in MEDIA_KINDS or key.partition(":")[0] in MEDIA_KINDS),
                        "Live media inventory is absent from fresh canonical proof")
        # Decoder scratch is exclusive and removed on every exit. Never use
        # the ordinary cache, which could alias a protected candidate input.
        scratch_root = ROOT / ".local"
        rt._require(scratch_root.resolve() == scratch_root, "Operator scratch root is linked")
        protected_dirs = [source.target] + [Path(p[1]).absolute() for p in json.loads(source.binding[7])]
        rt._require(all(scratch_root != p and p not in scratch_root.parents for p in protected_dirs),
                    "Operator scratch root is inside an input store")
        scratch_root.mkdir(parents=True, exist_ok=True)
        scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="media-adoption-", dir=scratch_root)))
        rt.validate_output_paths(source, [scratch], inputs)
        service = MediaIndexService(replace(config, qdrant=replace(config.qdrant, local_path=None)),
                                    ledger_path=ledger_path, client=client)
        service.preparation = MediaPreparationService(cache_dir=scratch)
        counts = {"ready": 0, "failed": 0, "excluded": 0}
        for key, evidence in sorted(sources.items()):
            kind, _, identifier = key.partition(":")
            ref = SourceRef(kind, identifier)
            resolver = AllReaderResolver(session, evidence, registration_hash)
            try:
                with _readonly_data_key_access():
                    result = await service.sync_source(resolver, ref, reuse_only=True)
            finally:
                resolver.close()
            rt._require(result["status"] == evidence["status"] and result.get("reason") == evidence["reason"],
                        "Fresh canonical media state differs; adoption stopped")
            counts[result["status"]] += 1
        verify_transferred(source, client, **verification)
        return {"media_adoption_complete": True, "media_sources": counts,
                "approved_gap_count": binding["approved_gap_count"],
                "transfer_binding_digest": rt.digest(binding), "production_cutover": "not_performed",
                "docs_knowledge_metadata": "not_changed", "queue": "not_changed"}
