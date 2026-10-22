"""Reconcile transferred media into the existing default live ledger.

All writers remain stopped throughout proof/transfer/adoption. Does not activate
runtime, acknowledge Docs/Knowledge metadata, or reset pending index queues.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.rag import runtime_transfer as rt  # noqa: E402


async def run(args):
    from qdrant_client import QdrantClient
    from src.memory.database import DatabaseManager
    from src.rag.runtime_media_adoption import adopt_media
    from src.rag.runtime_freeze_proof import (
        build_canonical_proof, registration_metadata_hash, registration_authority_digest,
    )
    from src.rag.shadow_migration import read_only_source_session
    from scripts.migrate_embeddinggemma2 import shadow_config

    db = DatabaseManager()
    try:
        source_binding = hashlib.sha256(json.dumps([
            db.config.postgres_host, db.config.postgres_port, db.config.postgres_db,
            db.config.postgres_schema], separators=(",", ":")).encode()).hexdigest()
        rt._require(source_binding == args.source_binding, "Canonical source database binding differs")
        async with read_only_source_session(db.SessionLocal) as session:
            config = await shadow_config(session)
            policies = rt.collection_policies(config.docs_collection_name, config.qdrant.collection_name,
                                             format_version=rt.candidate_format_version(args.manifest))
            proof = rt.read_json(args.canonical_proof)
            approval = rt.read_json(args.approval_manifest) if args.approval_manifest else None
            received = rt.read_json(args.approval_receipt) if args.approval_receipt else None
            with ExitStack() as stack:
                source = rt.ReadOnlyCandidate(args.candidate, args.manifest, args.generation, source_binding, policies)
                stack.callback(source.close)
                async def fresh_proof():
                    fresh = await build_canonical_proof(source, session_factory=db.SessionLocal,
                        config_reader=shadow_config, observed_source_binding=source_binding,
                        code_sha=args.code_sha, freeze_id=proof.get("freeze_id"),
                        confirm_all_writers_stopped=args.confirm_all_writers_stopped)
                    rt._require(rt.digest(fresh) == rt.digest(proof),
                                "Current canonical proof differs from the verified transfer")
                await fresh_proof()
                authority = await registration_authority_digest(session)
                async def registration_hash(session, ref):
                    return await registration_metadata_hash(session, ref, authority_digest=authority)
                client = QdrantClient(url=args.target_endpoint, api_key=config.qdrant.api_key, timeout=60)
                stack.callback(client.close)
                report = await adopt_media(source, client, session, config,
                    target_endpoint=args.target_endpoint, receipt_path=args.transfer_receipt,
                    canonical_proof=proof, code_sha=args.code_sha,
                    registration_hash=registration_hash,
                    approval_manifest=approval, approval_receipt=received,
                    confirm_all_writers_stopped=args.confirm_all_writers_stopped,
                    evidence_paths=[p for p in (args.canonical_proof, args.approval_manifest, args.approval_receipt) if p])
                rt._require(await registration_authority_digest(session) == authority,
                            "Canonical authority changed during media adoption")
                await fresh_proof()
                return report
    finally:
        await db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("candidate", "manifest", "transfer-receipt", "canonical-proof"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("generation", "source-binding", "target-endpoint", "code-sha"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--approval-manifest", type=Path)
    parser.add_argument("--approval-receipt", type=Path)
    parser.add_argument("--confirm-all-writers-stopped", action="store_true")
    args = parser.parse_args(argv)
    try:
        rt._require(args.confirm_all_writers_stopped, "Every canonical and index writer must remain stopped")
        rt._require(bool(args.approval_manifest) == bool(args.approval_receipt),
                    "Approval manifest and received approval evidence must be paired")
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
        rt._require(result.stdout.strip() == args.code_sha, "Running checkout differs from explicit code SHA")
        result = subprocess.run(["git", "status", "--porcelain", "--", "src", "scripts"],
                                cwd=ROOT, capture_output=True, text=True, check=True)
        rt._require(not result.stdout.strip(), "Operator checkout has uncommitted source changes")
        from src.security.field_crypto import _readonly_data_key_access
        with _readonly_data_key_access():
            report = asyncio.run(run(args))
        print(json.dumps(report, sort_keys=True))
        return 0
    except rt.TransferError as exc:
        error = str(exc)
    except Exception:
        error = "Media reconciliation failed; partial ledger progress may exist; activation refused"
    print(json.dumps({"media_adoption_complete": False, "production_cutover": "not_performed", "error": error}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
