"""Run actual EmbeddingGemma 2 + isolated Qdrant retrieval and resource checks.

No mock encoder is used and missing model/dependencies never produce PASS.
The default corpus is synthetic; --fixture may point to an authorized local
manifest with the same schema. No production index or original is modified.
Model downloads are handled separately; this evaluator loads local files only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _memory() -> dict[str, int | None]:
    import psutil
    info = psutil.Process().memory_info()
    peak = getattr(info, "peak_wset", None)
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    except ImportError:
        pass
    return {"rss_bytes": info.rss, "peak_rss_bytes": peak}


def _versions() -> dict[str, str | None]:
    result = {}
    for package in ["sentence-transformers", "transformers", "torch", "qdrant-client", "PyMuPDF"]:
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = None
    return result


def _latencies(values: list[float]) -> dict[str, float | int]:
    import numpy as np
    if not values:
        raise ValueError("Latency evaluation requires actual observations")
    return {"observations": len(values), "p50": float(np.percentile(values, 50)),
            "p95": float(np.percentile(values, 95)), "minimum": min(values), "maximum": max(values)}


def _synthetic_asset(document: dict[str, Any], work: Path) -> Path:
    """Generate only invented shapes/signals; filenames are fixture identifiers."""
    media = document["media"]
    kind = media["kind"]
    if kind == "image":
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (512, 512), "white")
        draw = ImageDraw.Draw(image)
        if media.get("shape") == "circle":
            draw.ellipse((100, 100, 412, 412), fill=media["color"])
        else:
            draw.rectangle((100, 100, 412, 412), fill=media["color"])
        path = work / f"{document['id']}.png"
        image.save(path)
    elif kind == "pdf":
        import fitz
        path = work / f"{document['id']}.pdf"
        with fitz.open() as pdf:
            page = pdf.new_page(width=400, height=200)
            page.insert_textbox(page.rect + (20, 20, -20, -20), document["text"], fontsize=16)
            pdf.save(path)
    elif kind == "audio":
        import numpy as np
        path = work / f"{document['id']}.wav"
        timeline = np.arange(round(media["seconds"] * 16_000)) / 16_000
        samples = (np.sin(2 * np.pi * media["frequency"] * timeline) * 12_000).astype("<i2")
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16_000)
            handle.writeframes(samples.tobytes())
    elif kind == "video":
        executable = shutil.which("ffmpeg")
        if not executable:
            raise RuntimeError("ffmpeg is required for actual video retrieval evaluation")
        path = work / f"{document['id']}.mkv"
        subprocess.run([
            executable, "-v", "error", "-nostdin", "-f", "lavfi", "-i",
            f"color=c={media['color']}:s=128x128:r=5", "-t", str(media["seconds"]),
            "-c:v", "ffv1", str(path),
        ], check=True, capture_output=True, timeout=180)
    else:
        raise ValueError(f"Unsupported fixture modality: {kind}")
    return path


async def evaluate(args: argparse.Namespace, work: Path) -> tuple[dict[str, Any], int]:
    import numpy as np
    from qdrant_client import QdrantClient, models
    from src.rag.config import EmbeddingConfig
    from src.rag.embedding import EmbeddingGemma2Embedding
    from src.rag.embedding_identity import DIMENSION, EMBEDDING_FINGERPRINT, EMBEDDING_IDENTITY
    from src.rag.media import MediaPreparationService, verify_full_coverage
    from src.rag.media_query import MediaQueryPreparer, QueryPlan, QuerySource, QuerySelector, PreparedQuery, require_prepared
    from src.services.search_sources import SourceRef, ResolvedSource, SourceUnavailable
    from src.services.unified_search import UnifiedSearchService

    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    report: dict[str, Any] = {
        "status": "NOT_VALIDATED", "embedding_identity": EMBEDDING_IDENTITY,
        "embedding_fingerprint": EMBEDDING_FINGERPRINT, "versions": _versions(), "versions_scope": "host_python",
        "scope": "isolated local fixture; production ACL and migration cutover require separate validation",
        "index_backend": "qdrant_local", "failures": [], "memory_before": _memory(),
    }
    encoder = EmbeddingGemma2Embedding(EmbeddingConfig(
        cache_dir=args.model_cache, device=args.device, batch_size=args.batch_size, local_files_only=True,
        worker_python=args.worker_python,
    ))
    client = None
    try:
        started = time.perf_counter()
        if not await encoder.initialize():
            report.update(status="BLOCKED_MODEL_UNAVAILABLE", model_error=encoder.last_error)
            return report, 2
        report["model_load_seconds"] = time.perf_counter() - started
        report["cold_startup_seconds"] = report["model_load_seconds"]
        report["precision"] = encoder.precision
        report["device"] = encoder.effective_device
        report["memory_after_load"] = _memory()
        report["worker_memory_after_load"] = dict(encoder.metrics)
        records = []
        original_paths = {}
        documents = {document["id"]: document for document in fixture["documents"]}
        service = MediaPreparationService(work / "media-cache")
        first_text_query = next(case["query"] for case in fixture["queries"] if case.get("query"))
        started = time.perf_counter()
        if await encoder.embed_query(first_text_query) is None:
            raise RuntimeError(f"cold query inference failed: {encoder.last_error}")
        report["cold_query_encode_seconds"] = time.perf_counter() - started
        started = time.perf_counter()
        for document in fixture["documents"]:
            if document.get("media"):
                # An external manifest's file_path must be an already authorized
                # local original. It is read only and never copied into reports.
                path = Path(document["file_path"]) if document.get("file_path") else _synthetic_asset(document, work)
                original_paths[document["id"]] = path
                media_chunks = service.prepare(
                    path, source_id=document["id"], metadata={"owner_user_id": "fixture-user",
                    "space_id": "fixture-space", "project_id": "fixture-project"},
                    mime_type=document.get("mime_type"), lexical_segments=document.get("lexical_segments", []),
                )
                inputs = [chunk.model_input for chunk in media_chunks]
                vectors = await encoder.encode_media(inputs)
                if len(vectors) != len(inputs):
                    raise RuntimeError(f"media inference failed: {encoder.last_error}")
                for chunk, vector in zip(media_chunks, vectors):
                    records.append({"source_id": document["id"], "vector": vector,
                                    "lexical": f"{document['title']}\n{chunk.lexical_text}",
                                    "locator": {key: value for key, value in chunk.metadata.items()
                                                if key in {"page_number", "frame_index", "start_seconds", "end_seconds", "modality"}}})
                by_modality: dict[str, list[Any]] = {}
                for chunk in media_chunks:
                    by_modality.setdefault(chunk.modality, []).append(chunk)
                for modality, chunks in by_modality.items():
                    if modality in {"audio", "video"}:
                        ranges = [(chunk.metadata["start_seconds"] - chunk.metadata.get("timeline_start_seconds", 0),
                                   chunk.metadata["end_seconds"] - chunk.metadata.get("timeline_start_seconds", 0))
                                  for chunk in chunks]
                        if not verify_full_coverage(ranges, chunks[-1].metadata["duration_seconds"]):
                            raise RuntimeError(f"Incomplete timeline: {document['id']} {modality}")
            else:
                path = Path(document["file_path"]) if document.get("file_path") else work / f"{document['id']}.txt"
                if not document.get("file_path"):
                    path.write_text(document["text"], encoding="utf-8")
                original_paths[document["id"]] = path
                vectors = await encoder.embed([document["text"]], titles=[document["title"]])
                if len(vectors) != 1:
                    raise RuntimeError(f"text inference failed: {encoder.last_error}")
                records.append({"source_id": document["id"], "vector": vectors[0],
                                "lexical": f"{document['title']}\n{document['text']}", "locator": {"modality": "text"}})
        report["corpus_encode_seconds"] = time.perf_counter() - started
        report["corpus_sources"] = len(fixture["documents"])
        report["corpus_vectors"] = len(records)
        report["estimated_float32_vector_bytes"] = len(records) * DIMENSION * 4
        client = QdrantClient(path=str(work / "qdrant"))
        collection = f"fixture__eg2_{EMBEDDING_FINGERPRINT[:16]}"
        client.create_collection(collection, vectors_config=models.VectorParams(size=DIMENSION, distance=models.Distance.COSINE))
        points = [models.PointStruct(id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"fixture:{index}")),
                                    vector=item["vector"], payload={
                                        "source_id": item["source_id"], "owner_user_id": "fixture-user",
                                        "space_id": "fixture-space", "project_id": "fixture-project",
                                        "lexical": item["lexical"], "locator": item["locator"],
                                    }) for index, item in enumerate(records)]
        # A private duplicate is closer than ordinary text candidates. Candidate
        # filtering must exclude it before limit, using all three scope fields.
        points.append(models.PointStruct(id=str(uuid.uuid5(uuid.NAMESPACE_URL, "private-decoy")),
                                        vector=records[0]["vector"], payload={"source_id": "private-decoy",
                                        "owner_user_id": "other-user", "space_id": "other-space",
                                        "project_id": "other-project"}))
        started = time.perf_counter()
        client.upsert(collection, points, wait=True)
        report["index_upsert_seconds"] = time.perf_counter() - started
        scope = models.Filter(must=[models.FieldCondition(key=key, match=models.MatchValue(value=value))
                                  for key, value in [("owner_user_id", "fixture-user"),
                                  ("space_id", "fixture-space"), ("project_id", "fixture-project")]])
        index_lock = threading.RLock()

        def search_dense(vector: list[float], query: str | None = None) -> tuple[list[str], dict[str, dict]]:
            # Match the application's local-client read/write coordination.
            with index_lock:
                hits = client.query_points(collection, query=vector, query_filter=scope,
                                           limit=len(records), with_payload=True).points
            if any(hit.payload["source_id"] == "private-decoy" for hit in hits):
                raise RuntimeError("Private fixture entered scoped candidates")
            dense = list(dict.fromkeys(hit.payload["source_id"] for hit in hits))
            # Exact identifiers keep their literal spelling outside embedding.
            literal = list(dict.fromkeys(item["source_id"] for item in records
                                        if query and query.casefold() in item["lexical"].casefold()))
            locators = {}
            for hit in hits:
                locators.setdefault(hit.payload["source_id"], hit.payload["locator"])
            return list(dict.fromkeys(literal + dense))[:args.top_k], locators

        def search(prepared, query=None):
            if not isinstance(prepared, PreparedQuery):
                return search_dense(prepared, query)  # separate CodeRetrieval comparison
            runs, locators = [], {}
            for unit in prepared.units:
                ranked, citations = search_dense(require_prepared(unit), query)
                hits = [{"target_kind": "fixture", "locator": {"kind": "fixture", "id": source_id},
                         "citation": citations[source_id]} for source_id in ranked]
                runs.append(("fixture", unit, hits))
                locators.update(citations)
            fused = UnifiedSearchService._fuse(runs, args.top_k)
            return [hit["locator"]["id"] for hit in fused], locators

        def acquire_original(source_id: str, locator: dict) -> int:
            """Reopen the original and reconstruct the returned page/time unit.

            This measures actual local original acquisition. Current DB ACL is
            deliberately outside this isolated fixture and is tested separately.
            """
            document, path = documents[source_id], original_paths[source_id]
            if not document.get("media"):
                return len(path.read_bytes())
            prepared = service.iter_prepare(path, source_id=source_id, mime_type=document.get("mime_type"))
            try:
                for chunk in prepared:
                    if chunk.modality != locator["modality"] or any(chunk.metadata.get(key) != value
                            for key, value in locator.items() if key != "modality"):
                        continue
                    if chunk.modality == "image":
                        import io
                        buffer = io.BytesIO()
                        chunk.model_input["image"].save(buffer, format="PNG")
                        return len(buffer.getvalue())
                    if chunk.modality == "audio":
                        return len(chunk.model_input["audio"].tobytes())
                    return len(Path(chunk.model_input["video"]).read_bytes())
                raise RuntimeError(f"Original unit locator unavailable: {source_id}")
            finally:
                prepared.close()

        refs = {identifier: SourceRef("knowledge_document", str(uuid.uuid5(uuid.NAMESPACE_URL,
                "aoitalk-evaluation:" + identifier))) for identifier in documents}
        class FixtureResolver:
            """Explicitly authorized isolated manifest, not application ACL evidence."""
            async def resolve(self, ref):
                identifier = next((key for key, value in refs.items() if value == ref), None)
                if identifier is None:
                    raise SourceUnavailable()
                path = original_paths[identifier]
                return ResolvedSource(ref, path, path.name)
        query_preparer = MediaQueryPreparer(encoder, service)
        async def encode_case(case):
            identifiers = case.get("query_media_ids") or ([case["query_media_id"]] if case.get("query_media_id") else [])
            selector = QuerySelector(**case.get("query_selector", {}))
            plan = QueryPlan(instruction=case.get("query", ""), mode=case.get("mode", "combined"),
                sources=tuple(QuerySource(ref=refs[identifier], selector=selector) for identifier in identifiers))
            return await query_preparer.prepare(plan, FixtureResolver())

        outcomes = []
        model_latencies = []
        search_latencies = []
        original_latencies = []
        code_comparison = []
        for case in fixture["queries"]:
            for repeat in range(args.repeats):
                started = time.perf_counter()
                vector = await encode_case(case)
                model_latencies.append(time.perf_counter() - started)
                started = time.perf_counter()
                ranked, locators = await asyncio.to_thread(search, vector,
                    case.get("query") if case["category"] == "identifier" else None)
                search_latencies.append(time.perf_counter() - started)
                started = time.perf_counter()
                acquired_bytes = await asyncio.to_thread(acquire_original, ranked[0], locators[ranked[0]])
                original_latencies.append(time.perf_counter() - started)
                if repeat == 0:
                    relevant = set(case["relevant_ids"])
                    first_rank = next((rank for rank, source_id in enumerate(ranked, 1) if source_id in relevant), None)
                    outcomes.append({"id": case["id"], "category": case["category"], "ranked_ids": ranked,
                                     "hit": first_rank is not None, "recall": len(set(ranked) & relevant) / len(relevant),
                                     "reciprocal_rank": 1 / first_rank if first_rank else 0,
                                     "original_unit_bytes": acquired_bytes, "original_locator": locators[ranked[0]],
                                     "query_source_status": vector.source_status, "query_units": len(vector.units)})
                    if case["category"] == "code":
                        code_vector = await encoder.embed_query(case["query"], code=True)
                        if code_vector is None:
                            raise RuntimeError(f"CodeRetrieval inference failed: {encoder.last_error}")
                        code_ranked, _ = search(code_vector)
                        code_rank = next((rank for rank, source_id in enumerate(code_ranked, 1) if source_id in relevant), None)
                        code_comparison.append({"query_id": case["id"], "SearchQuery_rank": first_rank,
                                                "CodeRetrieval_rank": code_rank, "CodeRetrieval_ids": code_ranked})
        categories = sorted({case["category"] for case in outcomes})
        report["quality"] = {category: {
            "queries": sum(case["category"] == category for case in outcomes),
            f"recall_at_{args.top_k}": float(np.mean([case["recall"] for case in outcomes if case["category"] == category])),
            "mrr": float(np.mean([case["reciprocal_rank"] for case in outcomes if case["category"] == category])),
        } for category in categories}
        report["queries"] = outcomes
        report["code_prefix_comparison"] = code_comparison
        report["latency_seconds"] = {
            "warm_query_encode": _latencies(model_latencies),
            "qdrant_plus_literal_retrieval": _latencies(search_latencies),
            "original_unit_acquisition": _latencies(original_latencies),
        }
        # Compete for the same resident encoder and update the same real local
        # collection while serving repeated queries and reopening original units.
        # The original corpus is unchanged, so correctness remains measurable.
        stop_updates = asyncio.Event()
        update_timings, update_failures = [], []
        target_index = next(i for i, record in enumerate(records) if record["locator"]["modality"] == "text")
        target_document = documents[records[target_index]["source_id"]]

        def upsert_update(vector):
            with index_lock:
                client.upsert(collection, [models.PointStruct(id=points[target_index].id, vector=vector,
                                                              payload=points[target_index].payload)], wait=True)

        async def update_index():
            try:
                while not stop_updates.is_set():
                    started = time.perf_counter()
                    vectors = await encoder.embed([target_document["text"]], titles=[target_document["title"]])
                    if len(vectors) != 1:
                        raise RuntimeError(f"concurrent index inference failed: {encoder.last_error}")
                    await asyncio.to_thread(upsert_update, vectors[0])
                    update_timings.append(time.perf_counter() - started)
                    await asyncio.sleep(0.01)
            except Exception as exc:
                update_failures.append(f"{type(exc).__name__}: {exc}")

        updater = asyncio.create_task(update_index())
        concurrent_encode, concurrent_search, concurrent_original = [], [], []
        try:
            await asyncio.sleep(0)
            for case in fixture["queries"]:
                for _ in range(args.repeats):
                    started = time.perf_counter()
                    vector = await encode_case(case)
                    concurrent_encode.append(time.perf_counter() - started)
                    started = time.perf_counter()
                    ranked, locators = await asyncio.to_thread(search, vector,
                        case.get("query") if case["category"] == "identifier" else None)
                    concurrent_search.append(time.perf_counter() - started)
                    started = time.perf_counter()
                    await asyncio.to_thread(acquire_original, ranked[0], locators[ranked[0]])
                    concurrent_original.append(time.perf_counter() - started)
                    if not set(ranked) & set(case["relevant_ids"]):
                        update_failures.append(f"concurrent retrieval miss: {case['id']}")
        finally:
            stop_updates.set()
            await updater
        report["concurrent_index_update"] = {
            "scope": "same shared encoder + same local Qdrant collection; local I/O serialized",
            "completed_updates": len(update_timings), "failures": update_failures,
            "update_seconds": _latencies(update_timings) if update_timings else None,
            "warm_query_encode_seconds": _latencies(concurrent_encode),
            "qdrant_plus_literal_retrieval_seconds": _latencies(concurrent_search),
            "original_unit_acquisition_seconds": _latencies(concurrent_original),
        }
        report["scope_fixture_private_candidate_count"] = 0
        if args.index_load_vectors:
            load_collection = collection + "_capacity"
            client.create_collection(load_collection, vectors_config=models.VectorParams(size=DIMENSION, distance=models.Distance.COSINE))
            # Reuse measured native vectors to isolate index insertion/search
            # overhead from encoder cost. This is local Qdrant capacity evidence,
            # not a server HNSW or production concurrency benchmark.
            started = time.perf_counter()
            for offset in range(0, args.index_load_vectors, 128):
                client.upsert(load_collection, [models.PointStruct(
                    id=index, vector=records[index % len(records)]["vector"],
                    payload={"source_id": records[index % len(records)]["source_id"]},
                ) for index in range(offset, min(offset + 128, args.index_load_vectors))], wait=True)
            upsert_seconds = time.perf_counter() - started
            load_query_seconds = []
            for _ in range(3):
                started = time.perf_counter()
                client.query_points(load_collection, query=records[0]["vector"], limit=args.top_k)
                load_query_seconds.append(time.perf_counter() - started)
            report["index_capacity_fixture"] = {
                "kind": "repeated_native_vectors_in_local_qdrant", "vectors": args.index_load_vectors,
                "upsert_seconds": upsert_seconds, "upsert_vectors_per_second": args.index_load_vectors / upsert_seconds,
                "query_p95_seconds": float(np.percentile(load_query_seconds, 95)),
                "float32_vector_bytes": args.index_load_vectors * DIMENSION * 4,
            }
        report["index_disk_bytes"] = sum(path.stat().st_size for path in (work / "qdrant").rglob("*") if path.is_file())
        report["memory_after_retrieval"] = _memory()
        report["worker_memory_after_retrieval"] = dict(encoder.metrics)
        if encoder.metrics.get("cuda_peak_bytes") is not None:
            report["cuda_peak_allocated_bytes"] = encoder.metrics["cuda_peak_bytes"]
        report["failures"] = [f"{category} recall below {args.minimum_recall}" for category, quality in report["quality"].items()
                              if quality[f"recall_at_{args.top_k}"] < args.minimum_recall]
        report["failures"].extend(update_failures)
        if not update_timings:
            report["failures"].append("no concurrent index update completed")
        report["status"] = "FAIL" if report["failures"] else "PASS_LOCAL_FIXTURE"
        return report, 1 if report["failures"] else 0
    finally:
        if client is not None:
            client.close()
        encoder.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests/fixtures/embeddinggemma2_retrieval.json")
    parser.add_argument("--model-cache", default=None)
    parser.add_argument("--worker-python", default=None, help="Python executable of the isolated embedding worker environment")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--minimum-recall", type=float, default=0.8)
    parser.add_argument("--index-load-vectors", type=int, default=2048)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/embeddinggemma2-evaluation.json")
    args = parser.parse_args()
    if min(args.batch_size, args.top_k, args.repeats) <= 0 or args.index_load_vectors < 0 or not 0 <= args.minimum_recall <= 1:
        parser.error("positive batch/top-k/repeats and minimum-recall within [0,1] are required")
    scratch = ROOT / ".local"
    scratch.mkdir(exist_ok=True)
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="eg2-evaluation-", dir=scratch) as directory:
            report, code = asyncio.run(evaluate(args, Path(directory)))
    except Exception as exc:
        report, code = {"status": "BLOCKED_OR_FAILED", "error": f"{type(exc).__name__}: {exc}",
                        "versions": _versions()}, 2
    report["wall_seconds"] = time.perf_counter() - started
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(args.output),
                      "failures": report.get("failures", []), "error": report.get("model_error", report.get("error"))},
                     ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
