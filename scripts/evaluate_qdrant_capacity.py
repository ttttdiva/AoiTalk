"""Measure an isolated local Qdrant server with synthetic 768d search inputs.

Never connects to an application database or production Qdrant. Collection
creation/removal is restricted to a random fixture collection on localhost:56333.
This capacity test does not measure model quality, worker encoding or SQL ACL.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import subprocess
import time
import uuid

import numpy as np
import httpx
import psutil
from qdrant_client import QdrantClient, models


def distribution(values):
    return {
        "observations": len(values),
        "p50_seconds": float(np.percentile(values, 50)),
        "p95_seconds": float(np.percentile(values, 95)),
    }


def run(args):
    client = QdrantClient(url="http://127.0.0.1:56333", timeout=180)
    collection = "eg2_fixture_capacity_" + uuid.uuid4().hex
    report = {
        "status": "FAIL",
        "synthetic_only": True,
        "backend": "Qdrant server HTTP; HNSW enabled",
        "server_version": client.info().version,
        "client_version": importlib.metadata.version("qdrant-client"),
        "vector_dimension": 768,
        "seed": 20261007,
        "scope": "synthetic unit vectors/payloads; no model encoding, SQL ACL, original bytes, or semantic-quality claim",
        "queries_return_bounded_top_k": True,
        "python_ranks_or_fetches_all_vectors": False,
    }
    created = False
    try:
        client.create_collection(
            collection,
            vectors_config={
                "dense": models.VectorParams(size=768, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={"sparse": models.SparseVectorParams()},
            shard_number=1,
            optimizers_config=models.OptimizersConfigDiff(indexing_threshold=0),
        )
        created = True
        for key in ("actor", "source_key", "embedding_fingerprint"):
            client.create_payload_index(
                collection, key, models.PayloadSchemaType.KEYWORD, wait=True
            )
        rng = np.random.default_rng(report["seed"])
        started = time.perf_counter()
        for offset in range(0, args.points, 1024):
            size = min(1024, args.points - offset)
            vectors = rng.standard_normal((size, 768), dtype=np.float32)
            vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
            points = [
                models.PointStruct(
                    id=i,
                    vector={
                        "dense": vectors[i - offset].tolist(),
                        "sparse": models.SparseVector(indices=[i % 257], values=[1.0]),
                    },
                    payload={
                        "actor": "fixture-allowed" if (i // 10) % 2 else "fixture-denied",
                        "source_key": f"fixture:{i // 10}",
                        "embedding_fingerprint": "eg2_fixture_768",
                        "input_hash": f"fixture:{i}",
                        "file_name": f"fixture_{i}.txt",
                    },
                )
                for i in range(offset, offset + size)
            ]
            client.upsert(collection, points, wait=True)
            if offset % 16384 == 0:
                print(json.dumps({"capacity_indexed": offset + size, "total": args.points}), flush=True)
        report["upsert_seconds"] = time.perf_counter() - started
        report["vectors_per_second"] = args.points / report["upsert_seconds"]
        started = time.perf_counter()
        client.update_collection(
            collection,
            optimizer_config=models.OptimizersConfigDiff(indexing_threshold=10000),
        )
        while True:
            info = client.get_collection(collection)
            if (
                info.status == models.CollectionStatus.GREEN
                and info.optimizer_status == models.OptimizersStatusOneOf.OK
                and info.indexed_vectors_count >= args.points * 0.95
            ):
                break
            if time.perf_counter() - started > 900:
                raise RuntimeError("HNSW indexing did not reach 95% before deadline")
            time.sleep(1)
        report["hnsw_build_seconds"] = time.perf_counter() - started
        report["points"] = client.count(collection, exact=True).count
        report["indexed_vectors"] = info.indexed_vectors_count
        report["indexed_vectors_includes_named_dense_and_sparse"] = True
        report["hnsw_config"] = info.config.hnsw_config.model_dump(mode="json")
        source_count = (args.points + 9) // 10
        allowed = [f"fixture:{i}" for i in range(source_count) if i % 2]
        report["source_count"] = source_count
        report["allowed_source_count"] = len(allowed)
        actor_filter = models.Filter(must=[models.FieldCondition(
            key="actor", match=models.MatchValue(value="fixture-allowed")
        )])
        source_filter = models.Filter(must=[
            models.FieldCondition(key="source_key", match=models.MatchAny(any=allowed)),
            models.FieldCondition(key="embedding_fingerprint", match=models.MatchValue(value="eg2_fixture_768")),
        ])
        query_vectors = rng.standard_normal((args.observations, 768), dtype=np.float32)
        query_vectors /= np.linalg.norm(query_vectors, axis=1, keepdims=True)
        matrix = {}
        for name, using, selected_filter in (
            ("dense_unfiltered", "dense", None),
            ("dense_actor_filter", "dense", actor_filter),
            ("dense_source_ids_filter", "dense", source_filter),
            ("sparse_unfiltered", "sparse", None),
            ("sparse_actor_filter", "sparse", actor_filter),
            ("sparse_source_ids_filter", "sparse", source_filter),
            ("hybrid_source_ids_filter", "hybrid", source_filter),
        ):
            values = []
            # One warmup per query shape is excluded from latency observations.
            for i in range(-1, args.observations):
                dense = query_vectors[max(i, 0)].tolist()
                sparse = models.SparseVector(indices=[max(i, 0) % 257], values=[1.0])
                kwargs = dict(query_filter=selected_filter, limit=10, with_payload=True, with_vectors=False)
                if using == "hybrid":
                    kwargs.update(
                        prefetch=[
                            models.Prefetch(query=dense, using="dense", filter=selected_filter, limit=40),
                            models.Prefetch(query=sparse, using="sparse", filter=selected_filter, limit=40),
                        ],
                        query=models.FusionQuery(fusion=models.Fusion.RRF),
                    )
                else:
                    kwargs.update(query=dense if using == "dense" else sparse, using=using)
                started = time.perf_counter()
                hits = client.query_points(collection, **kwargs).points
                elapsed = time.perf_counter() - started
                assert len(hits) == 10
                if selected_filter:
                    assert all(h.payload["actor"] == "fixture-allowed" for h in hits)
                if i >= 0:
                    values.append(elapsed)
            matrix[name] = distribution(values)
            print(json.dumps({"case": name, **matrix[name]}), flush=True)
        report["latency"] = matrix
        report["client_rss_bytes"] = psutil.Process().memory_info().rss
        report["server_memory_metrics_bytes"] = {
            line.split()[0]: int(line.split()[1])
            for line in httpx.get("http://127.0.0.1:56333/metrics", timeout=30).text.splitlines()
            if line.startswith(("memory_active_bytes ", "memory_allocated_bytes ", "memory_resident_bytes "))
        }
        if args.fixture_docker_metrics:
            # Only our fixed disposable container; never a user-supplied target.
            value = subprocess.check_output(
                ["docker", "exec", "aoitalk-eg2-qdrant", "du", "-sk", "/qdrant/storage"],
                text=True, timeout=30,
            )
            report["fixture_server_storage_bytes"] = int(value.split()[0]) * 1024
        report["status"] = "PASS_LOCAL_SERVER_FIXTURE"
        return report
    finally:
        if created:
            client.delete_collection(collection)
        client.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", type=int, default=145922)
    parser.add_argument("--observations", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-docker-metrics", action="store_true")
    args = parser.parse_args()
    if args.points < 10000 or args.observations < 3:
        parser.error("Use at least 10000 points and 3 observations")
    print(json.dumps(run(args), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
