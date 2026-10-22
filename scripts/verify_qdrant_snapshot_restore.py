"""Verify a downloaded Qdrant server snapshot by restoring it remotely.

The check is intentionally fail-closed. It only talks to the dedicated
allowlisted loopback HTTP fixture endpoints (56333, 56335, 56337, 56339). It never uses Qdrant
local mode and never treats a local file copy as a restore.

The source and destination collection names are generated for this run. A
snapshot is created and downloaded from the source server, uploaded to the
destination server, and then checked for collection schema, point count,
point IDs, payloads, vectors, and representative query results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx


SOURCE_HOST = "127.0.0.1"
SOURCE_PORT = 56333
DEFAULT_TARGET_PORT = 56335
DEFAULT_EXISTING_SOURCE_PORT = 56335
DEFAULT_EXISTING_TARGET_PORT = 56339
SOURCE_FIXTURE_PORTS = frozenset({56333, 56337})
TARGET_FIXTURE_PORTS = frozenset({56335, 56339, 56341})
FIXTURE_PORTS = SOURCE_FIXTURE_PORTS | TARGET_FIXTURE_PORTS
PRODUCTION_PORTS = frozenset({6333, 16333})
COLLECTION_PATTERN = re.compile(
    r"^aoitalk_snapshot_restore_[0-9a-f]{32}_(?:source|target)$"
)
POINT_COUNT = 64
VECTOR_SIZE = 1024
SEARCH_LIMIT = 5
CLEANUP_SUCCESS = frozenset({"deleted", "already_absent", "not_created"})


def _git_common_root(checkout_root: Path) -> Path | None:
    """Resolve only the canonical root associated with this checkout's git dir."""
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout_root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    common_dir = Path(result.stdout.strip()).resolve()
    if common_dir.name != ".git":
        return None
    root = common_dir.parent
    return root if (root / "AGENTS.md").is_file() else None


def _artifact_roots() -> tuple[Path, ...]:
    checkout_root = Path(__file__).resolve().parents[1]
    candidates = [checkout_root]
    canonical_root = _git_common_root(checkout_root)
    if canonical_root is not None and canonical_root != checkout_root:
        candidates.append(canonical_root)
    return tuple(root / "artifacts" for root in candidates if (root / "artifacts").is_dir())


class VerificationError(RuntimeError):
    """A sanitized, expected verification failure."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical(value).encode("utf-8"))


def _script_sha256() -> str:
    return _sha256_bytes(Path(__file__).read_bytes())


def _file_sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _snapshot_member_fingerprint(path: Path, member_name: str) -> dict[str, Any]:
    """Read metadata for one archive member without changing the snapshot."""
    try:
        with tarfile.open(path, mode="r:") as archive:
            member = archive.getmember(member_name)
            handle = archive.extractfile(member)
            if handle is None:
                return {"status": "missing_data"}
            data = handle.read()
    except (OSError, KeyError, tarfile.TarError) as exc:
        return {"status": f"unreadable_{type(exc).__name__}"}
    return {
        "status": "read",
        "bytes": len(data),
        "sha256": _sha256_bytes(data),
        "all_zero_bytes": bool(data) and not any(data),
    }


def _snapshot_member_json(path: Path, member_name: str) -> dict[str, Any] | None:
    try:
        with tarfile.open(path, mode="r:") as archive:
            handle = archive.extractfile(archive.getmember(member_name))
            if handle is None:
                return None
            value = json.loads(handle.read().decode("utf-8"))
    except (OSError, KeyError, UnicodeDecodeError, json.JSONDecodeError, tarfile.TarError):
        return None
    return value if isinstance(value, dict) else None


def _validate_existing_snapshot(path: Path, expected_sha256: str) -> tuple[Path, int, str, Path]:
    resolved = path.expanduser().resolve()
    artifact_root = next(
        (root for root in _artifact_roots() if _is_relative_to(resolved, root)),
        None,
    )
    if artifact_root is None:
        raise VerificationError("existing snapshot must be under a known repository artifacts root")
    if resolved.suffix.lower() != ".snapshot" or not resolved.is_file():
        raise VerificationError("existing snapshot file is missing or has an invalid extension")
    size, checksum = _file_sha256(resolved)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise VerificationError("expected snapshot SHA256 must be exactly 64 hexadecimal characters")
    if checksum.lower() != expected_sha256.lower():
        raise VerificationError("existing snapshot SHA256 differed from the supplied receipt")
    return resolved, size, checksum, artifact_root


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _endpoint(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def _validate_endpoints(
    source_host: str,
    source_port: int,
    target_host: str,
    target_port: int,
    *,
    same_server: bool,
    existing_snapshot: bool,
) -> None:
    source_ports = TARGET_FIXTURE_PORTS if existing_snapshot else SOURCE_FIXTURE_PORTS
    if source_host != SOURCE_HOST or source_port not in source_ports:
        raise VerificationError("source endpoint must be an allowlisted dedicated loopback fixture port")
    if target_host != SOURCE_HOST:
        raise VerificationError("target endpoint must be loopback")
    if target_port == source_port and not same_server:
        raise VerificationError("same-instance restore requires explicit --same-server")
    if target_port not in TARGET_FIXTURE_PORTS and not (same_server and target_port in SOURCE_FIXTURE_PORTS):
        raise VerificationError("target endpoint is outside the dedicated fixture port allowlist")
    if same_server and target_port != source_port:
        raise VerificationError("--same-server requires source and target ports to match")
    if source_port in PRODUCTION_PORTS or target_port in PRODUCTION_PORTS:
        raise VerificationError("production Qdrant port is rejected")


def _assert_owned_collection(name: str) -> None:
    if not COLLECTION_PATTERN.fullmatch(name):
        raise VerificationError("generated collection name failed the ownership guard")


def _request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> httpx.Response:
    try:
        response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise VerificationError(f"remote HTTP request failed: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        # Do not include a response body: native payloads and server details
        # must never enter the sanitized verification artifact.
        body = response.text.lower()
        suffix = " (qdrant_snapshot_wal_error)" if "wal error" in body or "first-index" in body else ""
        raise VerificationError(f"remote HTTP {response.status_code} for {method} {path}{suffix}")
    return response


def _json(client: httpx.Client, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    response = _request(client, method, path, **kwargs)
    try:
        value = response.json()
    except ValueError as exc:
        raise VerificationError(f"remote response was not JSON for {method} {path}") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"remote response shape was invalid for {method} {path}")
    return value


def _result(response: dict[str, Any], path: str) -> Any:
    if response.get("status") not in (None, "ok"):
        raise VerificationError(f"remote result status was invalid for {path}")
    if "result" not in response:
        raise VerificationError(f"remote result was missing for {path}")
    return response["result"]


def _server_version(client: httpx.Client) -> str:
    body = _json(client, "GET", "/")
    version = body.get("version")
    if not isinstance(version, str) or not version:
        raise VerificationError("Qdrant server version was missing")
    return version


def _collection_info(client: httpx.Client, name: str) -> dict[str, Any]:
    body = _json(client, "GET", f"/collections/{name}")
    result = _result(body, f"GET /collections/{name}")
    if not isinstance(result, dict):
        raise VerificationError("collection info had an invalid shape")
    return result


def _schema(info: dict[str, Any]) -> dict[str, Any]:
    config = info.get("config")
    if not isinstance(config, dict):
        raise VerificationError("collection schema did not include config")
    params = config.get("params")
    if not isinstance(params, dict):
        raise VerificationError("collection schema did not include params")
    # Snapshot restoration should preserve the collection's declared vector
    # and payload schema. Runtime counters and optimizer state are excluded.
    return {
        "vectors": params.get("vectors"),
        "sparse_vectors": params.get("sparse_vectors"),
        "payload_schema": info.get("payload_schema", {}),
    }


def _dense_vector(point: dict[str, Any]) -> tuple[str, list[float]]:
    vector = point.get("vector")
    if not isinstance(vector, dict):
        if isinstance(vector, list):
            return "", vector
        raise VerificationError("point did not contain a dense vector")
    for name, value in vector.items():
        if isinstance(value, list) and value and all(isinstance(v, (int, float)) for v in value):
            return name, value
    raise VerificationError("point did not contain a dense named vector")


def _point_for_compare(point: dict[str, Any]) -> dict[str, Any]:
    if "id" not in point or "payload" not in point or "vector" not in point:
        raise VerificationError("point response omitted id, payload, or vector")
    return {
        "id": str(point["id"]),
        "payload": point.get("payload"),
        "vector": point.get("vector"),
    }


def _scroll_all(client: httpx.Client, name: str) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    offset: Any = None
    while True:
        body: dict[str, Any] = {
            "limit": 128,
            "with_payload": True,
            "with_vector": True,
        }
        if offset is not None:
            body["offset"] = offset
        response = _json(
            client,
            "POST",
            f"/collections/{name}/points/scroll",
            json=body,
        )
        result = _result(response, f"POST /collections/{name}/points/scroll")
        if not isinstance(result, dict) or not isinstance(result.get("points"), list):
            raise VerificationError("scroll response had an invalid shape")
        points.extend(_point_for_compare(point) for point in result["points"])
        offset = result.get("next_page_offset")
        if offset is None:
            break
    return sorted(points, key=lambda point: point["id"])


def _point_digests(points: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [point["id"] for point in points]
    payloads = [{"id": point["id"], "payload": point["payload"]} for point in points]
    vectors = [{"id": point["id"], "vector": point["vector"]} for point in points]
    return {
        "count": len(points),
        "ids_sha256": _sha256_json(ids),
        "payloads_sha256": _sha256_json(payloads),
        "vectors_sha256": _sha256_json(vectors),
        "points_sha256": _sha256_json(points),
    }


def _create_fixture_points() -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for index in range(POINT_COUNT):
        # Use a deterministic dense basis with continuous values so the
        # representative top-k ordering has no tied zero-score tail. This
        # keeps search equality meaningful after a remote HNSW rebuild.
        rng = random.Random(0xA01 + index)
        vector = [rng.uniform(-1.0, 1.0) for _ in range(VECTOR_SIZE)]
        norm = math.sqrt(sum(value * value for value in vector))
        vector = [value / norm for value in vector]
        points.append(
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"aoitalk-snapshot-fixture:{index}")),
                "vector": vector,
                "payload": {
                    "doc_id": f"synthetic-{index}",
                    "actor": f"fixture-{index % 3}",
                    "title": f"snapshot fixture {index}",
                    "identifier": f"fixture://aoitalk/{index}",
                },
            }
        )
    return points


def _create_collection(client: httpx.Client, name: str) -> None:
    _assert_owned_collection(name)
    _request(
        client,
        "PUT",
        f"/collections/{name}",
        json={
            "vectors": {"size": VECTOR_SIZE, "distance": "Cosine"},
        },
    )


def _upsert(client: httpx.Client, name: str, points: list[dict[str, Any]]) -> None:
    _request(
        client,
        "PUT",
        f"/collections/{name}/points?wait=true",
        json={"points": points},
    )


def _upload_snapshot_file(client: httpx.Client, name: str, snapshot_file: Path) -> None:
    with snapshot_file.open("rb") as handle:
        _request(
            client,
            "POST",
            f"/collections/{name}/snapshots/upload?wait=true&priority=snapshot",
            files={"snapshot": (snapshot_file.name, handle, "application/octet-stream")},
        )


def _query(client: httpx.Client, name: str, query: list[float], using: str) -> list[dict[str, Any]]:
    body = {
        "query": query,
        "limit": SEARCH_LIMIT,
        "with_payload": False,
        "with_vector": False,
    }
    if using:
        body["using"] = using
    response = _json(client, "POST", f"/collections/{name}/points/query", json=body)
    result = _result(response, f"POST /collections/{name}/points/query")
    if isinstance(result, dict):
        result = result.get("points")
    if not isinstance(result, list):
        raise VerificationError("query response had an invalid shape")
    normalized = []
    for point in result:
        if not isinstance(point, dict) or "id" not in point or "score" not in point:
            raise VerificationError("query point omitted id or score")
        score = point["score"]
        if not isinstance(score, (int, float)) or not math.isfinite(float(score)):
            raise VerificationError("query point score was not finite")
        normalized.append({"id": str(point["id"]), "score": float(score)})
    return normalized


def _search_checks(
    source_client: httpx.Client,
    target_client: httpx.Client,
    source_name: str,
    target_name: str,
    source_points: list[dict[str, Any]],
    *,
    include_ids: bool = True,
) -> dict[str, Any]:
    if not source_points:
        raise VerificationError("representative search requires at least one point")
    anchors = [source_points[0], source_points[len(source_points) // 2], source_points[-1]]
    checks = []
    for point in anchors:
        using, query_vector = _dense_vector(point)
        source_hits = _query(source_client, source_name, query_vector, using)
        target_hits = _query(target_client, target_name, query_vector, using)
        ids_equal = [hit["id"] for hit in source_hits] == [hit["id"] for hit in target_hits]
        scores_equal = len(source_hits) == len(target_hits) and all(
            math.isclose(left["score"], right["score"], rel_tol=1e-9, abs_tol=1e-9)
            for left, right in zip(source_hits, target_hits)
        )
        if not ids_equal or not scores_equal:
            raise VerificationError("representative query results differed after restore")
        checks.append(
            {
                "anchor_id": point["id"] if include_ids else _sha256_json(point["id"]),
                "using": using,
                "source_ids": [hit["id"] for hit in source_hits] if include_ids else None,
                "restored_ids": [hit["id"] for hit in target_hits] if include_ids else None,
                "source_ids_sha256": _sha256_json([hit["id"] for hit in source_hits]),
                "restored_ids_sha256": _sha256_json([hit["id"] for hit in target_hits]),
                "source_scores_sha256": _sha256_json([hit["score"] for hit in source_hits]),
                "restored_scores_sha256": _sha256_json([hit["score"] for hit in target_hits]),
                "ids_equal": ids_equal,
                "scores_equal": scores_equal,
            }
        )
    return {"count": len(checks), "all_equal": True, "cases": checks}


def _resolve_ports(
    *,
    existing_snapshot: bool,
    source_port: int | None,
    target_port: int | None,
) -> tuple[int, int]:
    if existing_snapshot:
        return (
            source_port if source_port is not None else DEFAULT_EXISTING_SOURCE_PORT,
            target_port if target_port is not None else DEFAULT_EXISTING_TARGET_PORT,
        )
    return (
        source_port if source_port is not None else SOURCE_PORT,
        target_port if target_port is not None else DEFAULT_TARGET_PORT,
    )


def _owned_snapshot_name(
    collection: str,
    snapshot_name: str,
    allowed_collections: tuple[str, ...] = (),
) -> bool:
    owners = (collection,) + allowed_collections
    return any(snapshot_name.startswith(owner + "-") for owner in owners) and snapshot_name.endswith(
        ".snapshot"
    )


def _collection_absence(client: httpx.Client, name: str) -> str:
    for attempt in range(3):
        try:
            response = client.get(f"/collections/{name}")
        except httpx.HTTPError:
            return "verify_request_failed"
        if response.status_code == 404:
            return "absent"
        if response.status_code >= 400:
            return f"verify_http_{response.status_code}"
        if attempt < 2:
            time.sleep(0.1)
    return "still_present"


def _snapshot_absence(client: httpx.Client, collection: str, snapshot_name: str) -> str:
    for attempt in range(3):
        try:
            response = client.get(f"/collections/{collection}/snapshots")
        except httpx.HTTPError:
            return "verify_request_failed"
        if response.status_code == 404:
            return "absent"
        if response.status_code >= 400:
            return f"verify_http_{response.status_code}"
        try:
            snapshots = response.json().get("result", [])
            names = {
                item.get("name") for item in snapshots if isinstance(item, dict)
            }
        except (ValueError, AttributeError):
            return "verify_invalid_response"
        if snapshot_name not in names:
            return "absent"
        if attempt < 2:
            time.sleep(0.1)
    return "still_present"


def _cleanup_collection(client: httpx.Client, name: str) -> str:
    try:
        response = client.delete(f"/collections/{name}?wait=true")
    except httpx.HTTPError:
        return "request_failed"
    if response.status_code in (200, 404):
        absence = _collection_absence(client, name)
        if absence != "absent":
            return f"delete_{absence}"
        return "deleted" if response.status_code == 200 else "already_absent"
    return f"http_{response.status_code}"


def _cleanup_snapshot(
    client: httpx.Client,
    collection: str,
    snapshot_name: str | None,
    *,
    allowed_collections: tuple[str, ...] = (),
) -> str:
    if not snapshot_name:
        return "not_created"
    if not _owned_snapshot_name(collection, snapshot_name, allowed_collections):
        return "ownership_guard_failed"
    try:
        response = client.delete(
            f"/collections/{collection}/snapshots/{snapshot_name}?wait=true"
        )
    except httpx.HTTPError:
        return "request_failed"
    if response.status_code in (200, 404):
        absence = _snapshot_absence(client, collection, snapshot_name)
        if absence != "absent":
            return f"delete_{absence}"
        return "deleted" if response.status_code == 200 else "already_absent"
    return f"http_{response.status_code}"


def _cleanup_failures(cleanup: dict[str, str]) -> dict[str, str]:
    return {
        key: value for key, value in cleanup.items() if value not in CLEANUP_SUCCESS
    }


def _write_markdown(output: Path, report: dict[str, Any]) -> Path:
    markdown = output.with_suffix(".md")
    checks = report.get("checks", {})
    lines = [
        "# Qdrant snapshot restore verification",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Verification kind: `{report.get('verification_kind')}`",
        f"- Backend: `{report.get('backend_kind')}`",
        f"- Qdrant source: `{report.get('source_endpoint')}`",
        f"- Qdrant target: `{report.get('target_endpoint')}`",
        f"- Server versions: `{report.get('source_server_version')}` → `{report.get('target_server_version')}`",
        f"- Collections: `{report.get('source_collection')}` → `{report.get('target_collection')}`",
        "",
        "## Integrity checks",
        "",
        f"- Schema equal: `{checks.get('schema_equal')}`",
        f"- Count equal: `{checks.get('count_equal')}`",
        f"- IDs equal: `{checks.get('ids_equal')}`",
        f"- Payloads equal: `{checks.get('payloads_equal')}`",
        f"- Vectors equal: `{checks.get('vectors_equal')}`",
        f"- Representative searches equal: `{checks.get('search_equal')}`",
        "",
        "The artifact contains comparison hashes and identifiers; raw payload/vector values and credentials are omitted.",
        "",
    ]
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text("\n".join(lines), encoding="utf-8")
    return markdown


def run(
    output: Path,
    *,
    source_port: int | None = None,
    target_host: str = SOURCE_HOST,
    target_port: int | None = None,
    same_server: bool = False,
    snapshot_file: Path | None = None,
    expected_sha256: str | None = None,
) -> int:
    existing_snapshot = None
    if snapshot_file is not None:
        if expected_sha256 is None:
            raise VerificationError("--snapshot-file and --expected-sha256 must be supplied together")
        existing_snapshot = _validate_existing_snapshot(snapshot_file, expected_sha256)
    source_port, target_port = _resolve_ports(
        existing_snapshot=existing_snapshot is not None,
        source_port=source_port,
        target_port=target_port,
    )
    run_uuid = uuid.uuid4().hex
    prefix = f"aoitalk_snapshot_restore_{run_uuid}_"
    source_name = prefix + "source"
    target_name = prefix + "target"
    _assert_owned_collection(source_name)
    _assert_owned_collection(target_name)
    _validate_endpoints(
        SOURCE_HOST,
        source_port,
        target_host,
        target_port,
        same_server=same_server,
        existing_snapshot=bool(snapshot_file),
    )

    target_endpoint = _endpoint(target_host, target_port)
    # An existing snapshot is restored twice into new collections on the
    # dedicated target server. No production/source endpoint is contacted.
    source_endpoint = _endpoint(SOURCE_HOST, source_port)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshot_name: str | None = None
    source_client = httpx.Client(base_url=source_endpoint, timeout=180, trust_env=False)
    target_client = httpx.Client(base_url=target_endpoint, timeout=180, trust_env=False)
    verification_kind = (
        "existing_business_snapshot_server_restore" if existing_snapshot else "synthetic_server_fixture"
    )
    report: dict[str, Any] = {
        "schema_version": 2,
        "status": "FAIL",
        "verification_kind": verification_kind,
        "backend_kind": "qdrant_server_http",
        "restore_scope": (
            "existing_snapshot_cross_server"
            if existing_snapshot and source_port != target_port
            else ("existing_snapshot_same_server" if existing_snapshot else ("same_server_fixture" if same_server else "cross_server_fixture"))
        ),
        "source_endpoint": source_endpoint,
        "target_endpoint": target_endpoint,
        "source_collection": source_name,
        "target_collection": target_name,
        "run_uuid": run_uuid,
        "collection_prefix": prefix,
        "script_sha256": _script_sha256(),
        "local_snapshot_mode_used": False,
        "production_guard": {
            "source_fixed_to_dedicated_endpoint": not bool(existing_snapshot),
            "source_input_artifact_only": bool(existing_snapshot),
            "target_loopback_allowlisted": True,
            "generated_collection_prefix_guard": True,
            "production_endpoint_contacted": False if existing_snapshot else None,
        },
    }
    if existing_snapshot:
        resolved_snapshot, snapshot_bytes, snapshot_sha256, artifact_root = existing_snapshot
        archive_config = _snapshot_member_json(resolved_snapshot, "config.json") or {}
        archive_params = archive_config.get("params") if isinstance(archive_config, dict) else {}
        archive_params = archive_params if isinstance(archive_params, dict) else {}
        archive_vectors = archive_params.get("vectors")
        if isinstance(archive_vectors, dict) and "size" in archive_vectors:
            archive_vector_format = "unnamed_dense_float_vector"
            archive_dimension = archive_vectors.get("size")
        elif isinstance(archive_vectors, dict):
            archive_vector_format = "named_vector_map"
            archive_dimension = {
                name: value.get("size")
                for name, value in archive_vectors.items()
                if isinstance(value, dict) and "size" in value
            }
        else:
            archive_vector_format = "unknown"
            archive_dimension = None
        fingerprint_match = re.search(r"__eg2_([0-9a-f]{16})$", resolved_snapshot.stem)
        report["snapshot"] = {
            "name": resolved_snapshot.name,
            "bytes": snapshot_bytes,
            "sha256": snapshot_sha256,
            "expected_sha256": expected_sha256,
            "artifact_path": str(resolved_snapshot.relative_to(artifact_root.parent)),
            "input_existing_snapshot": True,
            "downloaded_from_source": False,
            "uploaded_to_source": False,
            "uploaded_to_target": False,
            "remote_restore_used": True,
            "archive_first_index": _snapshot_member_fingerprint(resolved_snapshot, "0/wal/first-index"),
            "archive_collection_schema": {
                "vector_format": archive_vector_format,
                "dimension": archive_dimension,
                "sparse_vector_names": sorted((archive_params.get("sparse_vectors") or {}).keys())
                if isinstance(archive_params.get("sparse_vectors"), dict)
                else [],
            },
        }
    try:
        source_version = _server_version(source_client)
        target_version = _server_version(target_client)
        report.update(source_server_version=source_version, target_server_version=target_version)
        existing_source = _json(source_client, "GET", "/collections")
        existing_target = _json(target_client, "GET", "/collections")
        source_result = _result(existing_source, "GET /collections")
        target_result = _result(existing_target, "GET /collections")
        source_names = {
            item.get("name") for item in (source_result or {}).get("collections", [])
            if isinstance(item, dict)
        }
        target_names = {
            item.get("name") for item in (target_result or {}).get("collections", [])
            if isinstance(item, dict)
        }
        if source_name in source_names or target_name in target_names:
            raise VerificationError("generated collection name was already present; refusing overwrite")

        if existing_snapshot:
            fixture = {
                "kind": "existing_business_snapshot",
                "input_snapshot_name": existing_snapshot[0].name,
                "native_qdrant_vector_format": archive_vector_format,
                "native_embedding_identity": {
                    "status": "metadata_only_not_reencoded",
                    "collection_fingerprint_prefix": fingerprint_match.group(1) if fingerprint_match else None,
                    "dimension": archive_dimension,
                },
                "native_embedding_vectors_verified": False,
                "source_collection_origin": existing_snapshot[0].stem,
                "raw_payloads_in_report": False,
            }
            report["fixture"] = fixture
            _upload_snapshot_file(source_client, source_name, existing_snapshot[0])
            report["snapshot"]["uploaded_to_source"] = True
            source_info_before = _collection_info(source_client, source_name)
            source_points = _scroll_all(source_client, source_name)
            if not source_points:
                raise VerificationError("existing snapshot restored zero points")
        else:
            points = _create_fixture_points()
            _create_collection(source_client, source_name)
            _upsert(source_client, source_name, points)
            source_info_before = _collection_info(source_client, source_name)
            source_points = _scroll_all(source_client, source_name)
            if len(source_points) != POINT_COUNT:
                raise VerificationError("source fixture point count did not match expected count")
            snapshot_response = _json(source_client, "POST", f"/collections/{source_name}/snapshots?wait=true")
            snapshot = _result(snapshot_response, f"POST /collections/{source_name}/snapshots")
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("name"), str):
                raise VerificationError("snapshot response did not include a name")
            snapshot_name = snapshot["name"]
            binary = _request(
                source_client,
                "GET",
                f"/collections/{source_name}/snapshots/{snapshot_name}",
            ).content
            checksum = _sha256_bytes(binary)
            server_checksum = snapshot.get("checksum")
            if server_checksum and server_checksum != checksum:
                raise VerificationError("downloaded snapshot checksum differed from server checksum")
            report["snapshot"] = {
                "name": snapshot_name,
                "bytes": len(binary),
                "sha256": checksum,
                "server_checksum": server_checksum,
                "downloaded_from_source": True,
                "uploaded_to_source": False,
                "uploaded_to_target": False,
                "remote_restore_used": True,
            }
            fixture = {
                "kind": "synthetic",
                "points": POINT_COUNT,
                "dense_dimension": VECTOR_SIZE,
                "named_vectors": [],
                "native_vectors_used": False,
            }
            report["fixture"] = fixture

        report["source_point_digests"] = _point_digests(source_points)
        report["source_schema_sha256"] = _sha256_json(_schema(source_info_before))
        if existing_snapshot:
            _upload_snapshot_file(target_client, target_name, existing_snapshot[0])
        else:
            _request(
                target_client,
                "POST",
                f"/collections/{target_name}/snapshots/upload?wait=true&priority=snapshot",
                files={"snapshot": (snapshot_name, binary, "application/octet-stream")},
            )
        report["snapshot"]["uploaded_to_target"] = True
        target_info_after = _collection_info(target_client, target_name)
        target_points = _scroll_all(target_client, target_name)
        source_digests = _point_digests(source_points)
        target_digests = _point_digests(target_points)
        schema_equal = _schema(source_info_before) == _schema(target_info_after)
        count_equal = source_digests["count"] == target_digests["count"]
        ids_equal = source_digests["ids_sha256"] == target_digests["ids_sha256"]
        payloads_equal = source_digests["payloads_sha256"] == target_digests["payloads_sha256"]
        vectors_equal = source_digests["vectors_sha256"] == target_digests["vectors_sha256"]
        if not all((schema_equal, count_equal, ids_equal, payloads_equal, vectors_equal)):
            raise VerificationError("restored collection integrity checks differed")
        search = _search_checks(
            source_client,
            target_client,
            source_name,
            target_name,
            source_points,
            include_ids=not bool(existing_snapshot),
        )
        report.update(
            source_point_digests=source_digests,
            target_point_digests=target_digests,
            source_schema_sha256=_sha256_json(_schema(source_info_before)),
            target_schema_sha256=_sha256_json(_schema(target_info_after)),
            checks={
                "schema_equal": schema_equal,
                "count_equal": count_equal,
                "ids_equal": ids_equal,
                "payloads_equal": payloads_equal,
                "vectors_equal": vectors_equal,
                "search_equal": search["all_equal"],
                "representative_search": search,
            },
            fixture=fixture,
        )
        if existing_snapshot:
            fixture["native_embedding_vectors_verified"] = True
        report["status"] = "PASS"
        return_code = 0
    except VerificationError as exc:
        report["error"] = {"type": "verification", "message": str(exc)}
        return_code = 1
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        report["error"] = {"type": type(exc).__name__, "message": "unexpected verification failure"}
        return_code = 1
    finally:
        cleanup = {
            "source_snapshot": _cleanup_snapshot(source_client, source_name, snapshot_name),
            "target_snapshot": _cleanup_snapshot(
                target_client,
                target_name,
                snapshot_name,
                allowed_collections=(source_name,),
            ),
            "source_collection": _cleanup_collection(source_client, source_name),
            "target_collection": _cleanup_collection(target_client, target_name),
        }
        report["cleanup"] = cleanup
        cleanup_failures = _cleanup_failures(cleanup)
        if cleanup_failures:
            report["status"] = "FAIL"
            report["cleanup_failure"] = cleanup_failures
            return_code = 1
        source_client.close()
        target_client.close()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["markdown_path"] = str(_write_markdown(output, report))
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return return_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--source-port",
        type=int,
        default=None,
        help="loopback source fixture port; defaults to 56333 synthetic or 56335 existing-snapshot",
    )
    parser.add_argument(
        "--target-port",
        type=int,
        default=None,
        help="loopback target fixture port; defaults to 56335 synthetic or 56339 existing-snapshot",
    )
    parser.add_argument(
        "--same-server",
        action="store_true",
        help="explicitly allow a same-instance synthetic fixture restore",
    )
    parser.add_argument(
        "--snapshot-file",
        type=Path,
        help="existing artifact snapshot to restore twice on the dedicated target server",
    )
    parser.add_argument(
        "--expected-sha256",
        help="receipt SHA256 required with --snapshot-file",
    )
    args = parser.parse_args(argv)
    try:
        if bool(args.snapshot_file) != bool(args.expected_sha256):
            raise VerificationError("--snapshot-file and --expected-sha256 must be supplied together")
        resolved_source, resolved_target = _resolve_ports(
            existing_snapshot=bool(args.snapshot_file),
            source_port=args.source_port,
            target_port=args.target_port,
        )
        if args.same_server and resolved_target != resolved_source:
            raise VerificationError("--same-server requires matching source and target ports")
        return run(
            args.output,
            source_port=args.source_port,
            target_port=args.target_port,
            same_server=args.same_server,
            snapshot_file=args.snapshot_file,
            expected_sha256=args.expected_sha256,
        )
    except VerificationError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
