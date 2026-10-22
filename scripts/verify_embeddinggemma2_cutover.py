"""Stopped-writer candidate transfer and verification; never activates production.

Canonical proof, approvals and receipts must remain private operator artifacts.
This CLI prints aggregate results and binding digests only, never source rows.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.rag.runtime_transfer import (  # noqa: E402
    ReadOnlyCandidate, TransferError, collection_policies, candidate_format_version, read_json, transfer,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--source-binding", required=True)
    parser.add_argument("--target-endpoint", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--canonical-proof", type=Path, required=True)
    parser.add_argument("--approval-manifest", type=Path)
    parser.add_argument("--approval-receipt", type=Path)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--docs-collection", default="aoitalk_docs")
    parser.add_argument("--knowledge-collection", default="aoitalk_knowledge")
    parser.add_argument("--confirm-all-writers-stopped", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.confirm_all_writers_stopped:
            raise TransferError("Every canonical and index writer must remain stopped")
        if bool(args.approval_manifest) != bool(args.approval_receipt):
            raise TransferError("Approval manifest and received approval evidence must be paired")
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
        if result.stdout.strip() != args.code_sha:
            raise TransferError("Running checkout differs from the explicit code SHA")
        result = subprocess.run(["git", "status", "--porcelain", "--", "src", "scripts/verify_embeddinggemma2_cutover.py"],
                                cwd=ROOT, capture_output=True, text=True, check=True)
        if result.stdout.strip():
            raise TransferError("Operator checkout has uncommitted source changes")
        proof = read_json(args.canonical_proof)
        approval = read_json(args.approval_manifest) if args.approval_manifest else None
        approval_receipt = read_json(args.approval_receipt) if args.approval_receipt else None
        policies = collection_policies(args.docs_collection, args.knowledge_collection,
                                       format_version=candidate_format_version(args.manifest))
        with ExitStack() as stack:
            source = ReadOnlyCandidate(args.candidate, args.manifest, args.generation, args.source_binding, policies)
            stack.callback(source.close)
            from qdrant_client import QdrantClient
            client = QdrantClient(url=args.target_endpoint, timeout=60)
            stack.callback(client.close)
            report = transfer(source, client, target_endpoint=args.target_endpoint, receipt_path=args.receipt,
                              canonical_proof=proof, code_sha=args.code_sha, approval_manifest=approval,
                              approval_receipt=approval_receipt, confirm_all_writers_stopped=True,
                              evidence_paths=[p for p in (args.canonical_proof, args.approval_manifest, args.approval_receipt) if p])
        print(json.dumps(report, sort_keys=True))
        return 0
    except TransferError as exc:
        print(json.dumps({"transfer_complete": False, "cutover_eligible_with_approved_gaps": False,
                          "production_cutover": "not_performed", "error": str(exc)}), file=sys.stderr)
        return 1
    except Exception:
        # SQL/client tracebacks may contain source text, payloads or private paths.
        print(json.dumps({"transfer_complete": False, "cutover_eligible_with_approved_gaps": False,
                          "production_cutover": "not_performed", "error": "Operator verification failed; receipt remains incomplete"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
