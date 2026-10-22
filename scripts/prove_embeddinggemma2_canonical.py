"""Create private canonical proof under an external all-writer freeze.

Never stops services, grants access, approves gaps, transfers points, or activates.
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


def check_checkout(code_sha):
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    rt._require(
        result.stdout.strip() == code_sha,
        "Running checkout differs from explicit code SHA",
    )
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", "src", "scripts"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    rt._require(
        not result.stdout.strip(), "Operator checkout has uncommitted source changes"
    )


async def run(args):
    from src.memory.database import DatabaseManager
    from scripts.migrate_embeddinggemma2 import shadow_config
    from src.rag.runtime_freeze_proof import build_canonical_proof, write_private_proof

    db = DatabaseManager()
    try:
        binding = hashlib.sha256(
            json.dumps(
                [
                    db.config.postgres_host,
                    db.config.postgres_port,
                    db.config.postgres_db,
                    db.config.postgres_schema,
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        rt._require(
            binding == args.source_binding, "Configured source database binding differs"
        )
        with ExitStack() as stack:
            source = rt.ReadOnlyCandidate(
                args.candidate,
                args.manifest,
                args.generation,
                binding,
                rt.collection_policies(args.docs_collection, args.knowledge_collection,
                                       format_version=rt.candidate_format_version(args.manifest)),
            )
            stack.callback(source.close)
            rt.validate_output_paths(source, [args.output])
            rt._require(
                not args.output.exists(),
                "Proof output already exists; use a fresh private filename",
            )
            proof = await build_canonical_proof(
                source,
                session_factory=db.SessionLocal,
                config_reader=shadow_config,
                observed_source_binding=binding,
                code_sha=args.code_sha,
                freeze_id=args.freeze_id,
                confirm_all_writers_stopped=args.confirm_all_writers_stopped,
            )
            check_checkout(args.code_sha)
            write_private_proof(source, args.output, proof)
        return {
            "canonical_proof_created": True,
            "source_count": len(proof["sources"]),
            "unit_count": len(proof["units"]),
            "failed_source_count": sum(
                s["status"] == "failed" for s in proof["sources"]
            ),
            "binding_digest": rt.digest(proof["binding"]),
            "proof_checksum": rt.digest(proof),
            "source_database": "read_only_rollback",
            "production_cutover": "not_performed",
        }
    finally:
        await db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("candidate", "manifest", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("generation", "source-binding", "code-sha", "freeze-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--docs-collection", default="aoitalk_docs")
    parser.add_argument("--knowledge-collection", default="aoitalk_knowledge")
    parser.add_argument("--confirm-all-writers-stopped", action="store_true")
    args = parser.parse_args(argv)
    try:
        rt._require(
            args.confirm_all_writers_stopped and bool(args.freeze_id.strip()),
            "Every canonical/index writer must remain stopped under external freeze ID",
        )
        check_checkout(args.code_sha)
        print(json.dumps(asyncio.run(run(args)), sort_keys=True))
        return 0
    except rt.TransferError as exc:
        error = str(exc)
    except (KeyboardInterrupt, asyncio.CancelledError):
        error = "Canonical verification cancelled; no proof was created"
    except Exception:
        error = "Fresh canonical verification failed; no approval or activation was performed"
    print(
        json.dumps(
            {
                "canonical_proof_created": False,
                "production_cutover": "not_performed",
                "error": error,
            }
        ),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
