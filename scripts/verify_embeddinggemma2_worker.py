"""Verify the real private worker's batches, crash, timeout and cancellation.

Uses the authorized local model cache only, and optionally measures a separate
synthetic Qdrant index at the supplied point count. Never opens an application
database, production index or user original.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def valid(vectors, count):
    assert len(vectors) == count
    assert all(
        len(v) == 768
        and all(math.isfinite(x) for x in v)
        and abs(sum(x * x for x in v) - 1) <= 0.01
        for v in vectors
    )


def capacity(vector, count):
    from qdrant_client import QdrantClient, models
    import numpy as np
    import psutil

    scratch = ROOT / ".local"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="eg2-capacity-", dir=scratch) as work:
        client = QdrantClient(path=str(Path(work) / "qdrant"))
        name = "fixture_native_768"
        client.create_collection(
            name,
            vectors_config={
                "dense": models.VectorParams(size=768, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={"sparse": models.SparseVectorParams()},
        )
        start = time.perf_counter()
        try:
            for offset in range(0, count, 1024):
                points = [
                    models.PointStruct(
                        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"eg2-fixture:{i}")),
                        vector={
                            "dense": vector,
                            "sparse": models.SparseVector(
                                indices=[i % 257], values=[1.0]
                            ),
                        },
                        payload={
                            "actor": "fixture-allowed" if i % 2 else "fixture-denied",
                            "input_hash": f"fixture:{i}",
                            "file_name": f"fixture_{i}.txt",
                        },
                    )
                    for i in range(offset, min(count, offset + 1024))
                ]
                client.upsert(name, points, wait=True)
                if offset % 16384 == 0:
                    print(
                        json.dumps(
                            {
                                "capacity_indexed": min(count, offset + 1024),
                                "total": count,
                            }
                        ),
                        flush=True,
                    )
            upsert_seconds = time.perf_counter() - start
            query_times = []
            allowed = models.Filter(
                must=[
                    models.FieldCondition(
                        key="actor", match=models.MatchValue(value="fixture-allowed")
                    )
                ]
            )
            for i in range(30):
                start = time.perf_counter()
                hits = client.query_points(
                    name, query=vector, using="dense", query_filter=allowed, limit=10
                ).points
                sparse_hits = client.query_points(
                    name,
                    query=models.SparseVector(indices=[i % 257], values=[1.0]),
                    using="sparse",
                    query_filter=allowed,
                    limit=10,
                ).points
                assert hits and sparse_hits
                assert all(
                    h.payload["actor"] == "fixture-allowed" for h in hits + sparse_hits
                )
                query_times.append(time.perf_counter() - start)
            return {
                "points": client.count(name, exact=True).count,
                "upsert_seconds": upsert_seconds,
                "vectors_per_second": count / upsert_seconds,
                "query_observations": len(query_times),
                "dense_plus_sparse_filtered_p50_seconds": float(
                    np.percentile(query_times, 50)
                ),
                "dense_plus_sparse_filtered_p95_seconds": float(
                    np.percentile(query_times, 95)
                ),
                "host_rss_bytes": psutil.Process().memory_info().rss,
                "index_disk_bytes": sum(
                    p.stat().st_size for p in Path(work).rglob("*") if p.is_file()
                ),
                "scope": "repeated native vector with synthetic payloads; capacity only, not semantic quality or live SQL ACL latency",
            }
        finally:
            client.close()


async def run(args):
    from src.rag.config import EmbeddingConfig
    from src.rag.embedding import EmbeddingGemma2Embedding
    from src.rag.embedding_identity import EMBEDDING_FINGERPRINT

    config = EmbeddingConfig(
        worker_python=args.worker_python,
        cache_dir=args.cache_dir,
        device=args.device,
        batch_size=8,
        local_files_only=True,
    )
    runtime = EmbeddingGemma2Embedding(config)
    report = {
        "identity": EMBEDDING_FINGERPRINT,
        "status": "FAIL",
        "synthetic_only": True,
    }
    try:
        start = time.perf_counter()
        assert await runtime.initialize(), runtime.last_error
        report["cold_load_seconds"] = time.perf_counter() - start
        vectors = await runtime.embed(
            [
                f"日本語の検索検証 {i}: RTX-830 192.0.2.17 RetryJob_42"
                for i in range(33)
            ],
            titles=[f"fixture_{i}.txt" for i in range(33)],
        )
        valid(vectors, 33)
        report["bounded_batch_documents"] = 33
        previous = runtime._process
        previous.kill()
        await asyncio.to_thread(previous.wait)
        start = time.perf_counter()
        vector = await runtime.embed_query("型番とIPアドレスを探す")
        valid([vector] if vector else [], 1)
        assert runtime._process is not previous
        report["crash_recovery_seconds"] = time.perf_counter() - start
        previous = runtime._process
        request = asyncio.create_task(
            runtime.embed(["長い処理の取消検証 " * 4000] * 32)
        )
        await asyncio.sleep(0.02)
        start = time.perf_counter()
        request.cancel()
        try:
            await request
            raise AssertionError("Cancellation was swallowed")
        except asyncio.CancelledError:
            pass
        assert previous.poll() is not None and runtime._process is None
        report["cancellation_drained_seconds"] = time.perf_counter() - start
        valid([await runtime.embed_query("取消後も検索できる")], 1)
        runtime.config.request_timeout_seconds = 0.001
        assert await runtime.embed_query("期限切れを検証する") is None
        assert "timeout" in (runtime.last_error or "").lower()
        assert runtime._process is None
        report["timeout_error"] = runtime.last_error
        runtime.config.request_timeout_seconds = 180
        valid([await runtime.embed_query("期限切れ後の検索を再開する")], 1)
        report["timeout_recovery"] = True
        report["worker_metrics"] = runtime.metrics
        if args.capacity_points:
            report["capacity"] = await asyncio.to_thread(
                capacity, vector, args.capacity_points
            )
        report["status"] = "PASS_LOCAL_FIXTURE"
        return report
    finally:
        runtime.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-python", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capacity-points", type=int, default=0)
    args = parser.parse_args()
    if args.capacity_points < 0:
        parser.error("--capacity-points must be nonnegative")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
