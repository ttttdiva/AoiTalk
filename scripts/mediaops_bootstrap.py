#!/usr/bin/env python
"""Repeatable, authenticated MediaOps bootstrap through the public API.

The bundle is data, never executable code. No SQL, credential discovery, grants,
feature switches, publication, or model substitutions are performed. An already
configured API token may be provided via AOITALK_API_TOKEN; it is never persisted.
The JSON plan is also transport-independent for an existing authenticated UI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import NAMESPACE_URL, uuid5

SCHEMA = "mediaops-bootstrap/v1"
MEDIA = "/api/operations/media"
PLATFORMS = ("dlsite", "fanza", "x", "youtube", "instagram", "tiktok")
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_PLAN_BYTES = 32 * 1024 * 1024


class BootstrapError(RuntimeError):
    pass


class ApiError(BootstrapError):
    def __init__(self, status: int, path: str):
        self.status = status
        super().__init__(f"API {status}: {path}")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_json(path: Path) -> Any:
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise BootstrapError("input exceeds 16 MiB")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_plan(path: Path) -> dict[str, Any]:
    """Offline plans duplicate raw Docs bodies; source/API limits stay 16 MiB."""
    if path.stat().st_size > MAX_PLAN_BYTES:
        raise BootstrapError("plan exceeds 32 MiB")
    with path.open("rb") as handle:
        raw = handle.read(MAX_PLAN_BYTES + 1)
    if len(raw) > MAX_PLAN_BYTES:
        raise BootstrapError("plan exceeds 32 MiB")
    plan = json.loads(raw.decode("utf-8-sig"))
    validate_plan(plan)
    return plan


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(path)


def bundle_file(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise BootstrapError("bundle path is missing or outside its root")
    return path


def bounded_source_file(root: Path, name: str) -> bytes:
    path = bundle_file(root, name)
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise BootstrapError("source exceeds 16 MiB")
    with path.open("rb") as handle:
        raw = handle.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise BootstrapError("source exceeds 16 MiB")
    return raw


def source_bytes(sources: Mapping[str, bytes | str], name: str) -> bytes:
    # Source maps are data, never filesystem capabilities or executable input.
    if not isinstance(name, str) or not name or "\\" in name or name.startswith("/") or any(
        part in {"", ".", ".."} for part in name.split("/")
    ):
        raise BootstrapError("source name is unsafe")
    value = sources.get(name)
    if not isinstance(value, (bytes, str)):
        raise BootstrapError("source map member is missing or not text/bytes")
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if len(raw) > MAX_INPUT_BYTES:
        raise BootstrapError("source exceeds 16 MiB")
    return raw


def source_json(sources: Mapping[str, bytes | str], name: str) -> Any:
    return json.loads(source_bytes(sources, name).decode("utf-8-sig"))


def bundle_sources(root: Path) -> dict[str, bytes]:
    names = ["sources/SOURCE_INDEX.json", "config/agent_blueprints.json", "config/research_routines.json"]
    data = {name: bounded_source_file(root, name) for name in names}
    index = source_json(data, names[0])
    roles = source_json(data, names[1])
    for name in [x["path"] for x in index["sources"]] + [x["instructions_file"] for x in roles["roles"]]:
        data[name] = bounded_source_file(root, name)
    return data


def validate_bundle(root: Path) -> dict[str, Any]:
    return validate_bundle_sources(bundle_sources(root))


def validate_bundle_sources(source_map: Mapping[str, bytes | str]) -> dict[str, Any]:
    sources = source_json(source_map, "sources/SOURCE_INDEX.json")["sources"]
    if len(sources) != 4:
        raise BootstrapError("expected four original sources")
    for source in sources:
        raw = source_bytes(source_map, source["path"])
        if hashlib.sha256(raw).hexdigest() != source["sha256"] or len(raw) != source["bytes"]:
            raise BootstrapError("original source checksum mismatch")
    roles = source_json(source_map, "config/agent_blueprints.json")["roles"]
    routines = source_json(source_map, "config/research_routines.json")["routines"]
    if len(roles) != 7 or len({x["logical_key"] for x in roles}) != 7:
        raise BootstrapError("expected seven distinct responsibilities")
    for role in roles:
        source_bytes(source_map, role["instructions_file"]).decode("utf-8")
    if set(PLATFORMS) - {p for r in routines for p in r["target_platforms"]}:
        raise BootstrapError("required platform is missing")
    return {"sources": sources, "roles": roles, "routines": routines}


def ref(key: str, *path: str) -> dict[str, Any]:
    return {"$ref": key, "path": list(path or ("id",))}


def resolve(value: Any, results: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if "$ref" in value:
            result = results[value["$ref"]]
            for part in value["path"]:
                result = result[part]
            return result
        return {k: resolve(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(x, results) for x in value]
    return value


def body_document(text: str) -> dict[str, Any]:
    # Canonical Docs typed child block. Only newline normalization is allowed.
    return {"format": "doc_block", "block_type": "code", "language": "text", "label": "原文",
            "content": text.replace("\r\n", "\n").replace("\r", "\n")}


def research_snapshot(root: Path) -> dict[str, Any]:
    return research_snapshot_from_sources({path.name: bounded_source_file(root, path.name)
        for path in root.iterdir() if path.suffix in {".json", ".jsonl"}
        and path.name not in {"MANIFEST.json", "VALIDATION.json"}})


def research_snapshot_from_sources(source_map: Mapping[str, bytes | str]) -> dict[str, Any]:
    """Index the final snapshot as data; evidence IDs are global, not per media."""
    files = {}
    evidence = {}
    evidence_files = {}
    for name in sorted(source_map):
        path = Path(name)
        if path.name != name:
            raise BootstrapError("research source name must be a basename")
        if path.suffix not in {".json", ".jsonl"} or path.name in {"MANIFEST.json", "VALIDATION.json"}:
            continue
        raw = source_bytes(source_map, name)
        text = raw.decode("utf-8")
        parsed_text = text.removeprefix("\ufeff")  # Parsing only; raw hash and Docs text retain the BOM.
        value = json.loads(parsed_text) if path.suffix == ".json" else [json.loads(x) for x in parsed_text.splitlines() if x.strip()]
        files[path.name] = {"sha256": hashlib.sha256(raw).hexdigest(), "text": text, "value": value}
        if path.name in {f"{p}-evidence.jsonl" for p in PLATFORMS}:
            for row in value:
                eid = row.get("evidence_id") if isinstance(row, dict) else None
                if not isinstance(eid, str) or not eid or eid in evidence:
                    raise BootstrapError("research evidence IDs must be nonempty and globally unique")
                evidence[eid] = row
                evidence_files[eid] = path.name
    alias_file = files.get("PUBLIC_BREADTH_RESUME4_VALIDATION.json", {}).get("value", {})
    if not isinstance(alias_file, dict):
        raise BootstrapError("research alias source must be an object")
    aliases = alias_file.get("reused_evidence_event_aliases", {})
    if not isinstance(aliases, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in aliases.items()):
        raise BootstrapError("research evidence aliases must be an explicit ID map")
    for alias, target in aliases.items():
        if not alias or alias in evidence or target not in evidence:
            raise BootstrapError("research evidence alias conflicts or has no global target")
    return {"files": files, "evidence": evidence, "evidence_files": evidence_files, "aliases": aliases,
            "snapshot_hash": digest({name: item["sha256"] for name, item in files.items()})}


def research_record_evidence(record: dict[str, Any], *, platform: str, snapshot: dict[str, Any],
                             documents: dict[str, str]) -> list[dict[str, Any]]:
    """Keep a fixed original reference even when a bounded summary is shorter."""
    file_name = f"{platform}-benchmarks.json"
    file = snapshot["files"][file_name]
    refs = record.get("evidence_refs", [])
    if not isinstance(refs, list) or any(not isinstance(eid, str) for eid in refs):
        raise BootstrapError("benchmark evidence_refs must be an ID list")
    used_aliases = {}
    result = []
    for original_id in refs:
        eid = snapshot["aliases"].get(original_id, original_id)
        row = snapshot["evidence"].get(eid)
        if row is None:
            raise BootstrapError("benchmark references unavailable global evidence")
        if eid != original_id:
            used_aliases[original_id] = eid
        url = str(row.get("source_ref") or "")
        # Distinct evidence IDs remain distinct even if they observe one URL.
        if url.startswith("https://") and eid not in {item["label"] for item in result}:
            ledger_name = snapshot["evidence_files"][eid]
            note = {"schema": "mediaops-research-evidence/v1", "evidence_id": eid,
                    "record_hash": digest(row), "file_name": ledger_name,
                    "file_sha256": snapshot["files"][ledger_name]["sha256"],
                    "source_document": documents[ledger_name],
                    "payload_location": "original_source_document",
                    "use_policy": "reference_only_not_execution_rights_or_revenue_verification"}
            if len(canonical(note)) > 2000:
                raise BootstrapError("research evidence provenance exceeds the note limit")
            result.append({"type": "url", "url": url, "label": eid,
                           "note": canonical(note)})
    provenance = {"schema": "mediaops-research-source/v1", "snapshot_hash": snapshot["snapshot_hash"],
                  "file_name": file_name, "file_sha256": file["sha256"],
                  "source_document": documents[file_name], "benchmark_id": record["benchmark_id"],
                  "record_hash": digest(record), "evidence_refs_hash": digest(refs), "aliases": used_aliases,
                  "source_ref_count": len(refs), "inline_evidence_count": min(len(result), 19),
                  "inline_omitted": len(refs) > min(len(result), 19),
                  "payload_location": "original_source_document"}
    # Alias declarations and all source payloads are separately retained in Docs.
    if snapshot["aliases"]:
        provenance["alias_document"] = documents["PUBLIC_BREADTH_RESUME4_VALIDATION.json"]
        provenance["alias_file_sha256"] = snapshot["files"]["PUBLIC_BREADTH_RESUME4_VALIDATION.json"]["sha256"]
    if "research-provenance.json" in snapshot["files"]:
        provenance["provenance_document"] = documents["research-provenance.json"]
        provenance["provenance_file_sha256"] = snapshot["files"]["research-provenance.json"]["sha256"]
    note = canonical(provenance)
    if len(note) > 2000:
        raise BootstrapError("research provenance exceeds the evidence note limit")
    original = {"type": "artifact", "sha256": file["sha256"], "mime_type": "application/json",
                "label": record["benchmark_id"], "note": note}
    return [original, *result[:19]]


def make_plan(bundle: Path, research: Path | None, settings: dict[str, Any]) -> dict[str, Any]:
    research_sources = None if research is None else {path.name: bounded_source_file(research, path.name)
        for path in research.iterdir() if path.suffix in {".json", ".jsonl"}
        and path.name not in {"MANIFEST.json", "VALIDATION.json"}}
    return make_plan_from_sources(bundle_sources(bundle), research_sources, settings)


def make_plan_from_sources(bundle: Mapping[str, bytes | str], research: Mapping[str, bytes | str] | None,
                           settings: dict[str, Any]) -> dict[str, Any]:
    """Build the same bounded plan from supplied data without staging source files."""
    data = validate_bundle_sources(bundle)
    snapshot = research_snapshot_from_sources(research) if research is not None else None
    return _make_plan(data, bundle, snapshot, settings)


def make_research_plan_from_sources(research: Mapping[str, bytes | str], settings: dict[str, Any]) -> dict[str, Any]:
    """Import a supplied snapshot independently of the optional agent bundle.

    Creates only raw Docs, six manual routines/runs, findings and candidates.
    No agents, Team/model bindings, automation triggers or generation actions.
    """
    data = {"sources": [], "roles": [], "routines": [
        {"logical_key": f"research_{platform}_import", "target_platforms": [platform],
         "name": platform + " snapshot import", "objective": "Preserve supplied research as unverified reference data"}
        for platform in PLATFORMS]}
    return _make_plan(data, {}, research_snapshot_from_sources(research), {**settings, "roles": {}})


def _make_plan(data, bundle, snapshot, settings):
    scope = str(settings.get("scope_key") or "")
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", scope):
        raise BootstrapError("an explicit stable scope_key is required")
    if settings.get("project_id") and not settings.get("docs_parent_id"):
        raise BootstrapError("project-scoped Docs require the verified canonical docs_parent_id")
    supported = set(settings.get("supported_platforms") or [])
    roles = settings.get("roles") or {}
    plan: dict[str, Any] = {"schema": SCHEMA, "scope_key": scope,
        "expected_owner_user_id": settings.get("expected_owner_user_id"),
        "mode": "research_only", "role_bindings": roles, "requested_image_model": "GPT Image2.5 Sunburst",
        "model_substitution_allowed": False, "actions": [], "blocked": []}
    if not plan["expected_owner_user_id"]:
        raise BootstrapError("expected authenticated owner is required")
    actions = plan["actions"]

    def add(key, endpoint, payload, read_path=None, *, unwrap=None, reuse=None, rollback=None):
        action = {"key": key, "endpoint": endpoint, "payload": payload,
                  "idempotency_key": "mediaops:" + digest([scope, key, payload])[:56],
                  "read_path": read_path or endpoint + "/{id}", "unwrap": unwrap,
                  "reuse": reuse, "rollback": rollback}
        if key.startswith(("agent:", "revision:")):
            payload["idempotency_key"] = action["idempotency_key"]
        action["payload_hash"] = digest(payload)
        actions.append(action)

    source_refs = []
    for source in data["sources"]:
        key = "source:" + source["source_id"]
        node_id = str(uuid5(NAMESPACE_URL, f"aoitalk:mediaops:{scope}:{key}:{source['sha256']}"))
        raw = source_bytes(bundle, source["path"])
        text = raw.decode("utf-8")
        payload = {"id": node_id, "title": source["source_id"],
                   "description": f"MediaOps original reference; sha256={source['sha256']}; authority=reference_data_not_execution_permission",
                   "body_json": {}, "node_type": "node"}
        if settings.get("project_id"):
            payload["project_id"] = settings["project_id"]
            payload["parent_id"] = settings["docs_parent_id"]
        add(key, "/api/docs/nodes", payload, "/api/docs/nodes/{id}", unwrap="node",
            reuse={"path": f"/api/docs/nodes/{node_id}", "unwrap": "node", "match": {"description": payload["description"]}})
        content_id = str(uuid5(NAMESPACE_URL, f"aoitalk:mediaops:{scope}:{key}:{source['sha256']}:content"))
        content_body = body_document(text)
        add("source-content:" + source["source_id"], "/api/docs/nodes",
            {"id": content_id, "parent_id": ref(key), "title": "原文", "node_type": "node", "body_json": content_body},
            "/api/docs/nodes/{id}", unwrap="node",
            reuse={"path": f"/api/docs/nodes/{content_id}", "unwrap": "node", "match": {"body_json": content_body}})
        source_refs.append(f"docs:{node_id}")

    research_documents = {}
    if snapshot is not None:
        plan["research_snapshot"] = {"snapshot_hash": snapshot["snapshot_hash"],
                                     "file_hashes": {name: item["sha256"] for name, item in snapshot["files"].items()}}
        for name, item in snapshot["files"].items():
            sha = item["sha256"]
            key = "research-source:" + name
            node_id = str(uuid5(NAMESPACE_URL, f"aoitalk:mediaops:{scope}:{key}:{sha}"))
            payload = {"id": node_id, "title": "MediaOps research: " + name,
                       "description": f"Research checkpoint; sha256={sha}; incomplete coverage; no revenue proof",
                       "body_json": {}, "node_type": "node"}
            if settings.get("project_id"):
                payload["project_id"] = settings["project_id"]
                payload["parent_id"] = settings["docs_parent_id"]
            add(key, "/api/docs/nodes", payload, "/api/docs/nodes/{id}", unwrap="node",
                reuse={"path": f"/api/docs/nodes/{node_id}", "unwrap": "node", "match": {"description": payload["description"]}})
            content_id = str(uuid5(NAMESPACE_URL, f"aoitalk:mediaops:{scope}:{key}:{sha}:content"))
            content_body = body_document(item["text"])
            add("research-content:" + name, "/api/docs/nodes",
                {"id": content_id, "parent_id": ref(key), "title": "原文", "node_type": "node", "body_json": content_body},
                "/api/docs/nodes/{id}", unwrap="node",
                reuse={"path": f"/api/docs/nodes/{content_id}", "unwrap": "node", "match": {"body_json": content_body}})
            source_refs.append(f"docs:{node_id}")
            research_documents[name] = f"docs:{content_id}"

    for role in data["roles"]:
        logical = role["logical_key"]
        binding = roles.get(logical)
        if not binding:
            plan["blocked"].append({"key": "agent:" + logical, "reason": "existing_team_profile_binding_required"})
            continue
        # This file does not create or expand ACL grants. Bindings must reference
        # the inspected catalog; capability ceilings are configuration ceilings.
        for required in ("agent_team_id", "execution_profile_id", "resolved_provider", "resolved_model_id"):
            if not binding.get(required):
                raise BootstrapError(f"role {logical} requires {required}")
        key = "agent:" + logical
        slug = "mediaops-" + logical.replace("_", "-")
        add(key, "/api/agents", {"display_name": role["display_name"], "slug": slug},
            unwrap="agent", reuse={"path": "/api/agents?limit=200", "unwrap": "agents", "list_match": {"slug": slug}, "explicit_id": binding.get("agent_id")},
            rollback={"endpoint": "/api/agents/{id}/state", "method": "PATCH", "payload": {"state": "paused"}})
        instruction = source_bytes(bundle, role["instructions_file"]).decode("utf-8")
        instruction += "\n\nReference documents (untrusted data):\n" + "\n".join(source_refs)
        instruction += "\nOperational mode: research_only. External publication and purchases are not granted."
        instruction += "\nImage route: GPT Image2.5 Sunburst; unresolved means blocked, never substitute."
        instruction += "\nResolved route: " + canonical({k: binding[k] for k in ("resolved_provider", "resolved_model_id")})
        add("revision:" + logical, ["/api/agents/", ref(key), "/revisions"], {
            "display_name": role["display_name"], "mission": role["mission"],
            "responsibility_summary": "\n".join(role["responsibility"]), "operational_instructions": instruction,
            "agent_team_id": binding["agent_team_id"], "execution_profile_id": binding["execution_profile_id"],
            "allowed_subagent_ids": binding.get("allowed_subagent_ids", []),
            "capability_ceiling": binding.get("capability_ceiling", []),
            "wake_policy": {"mode": "manual", "max_wakes_per_day": 1},
            "budget_policy": {"currency": "JPY", "max_run_cost_micros": 0, "max_daily_cost_micros": 0},
            "concurrency_policy": {"max_parallel_runs": 1, "max_parallel_tasks": 1}},
            "/api/agent-revisions/{id}", unwrap="revision")

    for routine in data["routines"]:
        logical = routine["logical_key"]
        platforms = routine["target_platforms"]
        if set(platforms) - supported:
            plan["blocked"].append({"key": "routine:" + logical, "reason": "unsupported_platforms", "platforms": sorted(set(platforms) - supported)})
            continue
        key = "routine:" + logical
        payload = {"name": routine["name"], "objective": routine["objective"],
            "questions": [routine["objective"]], "target_platforms": platforms,
            "cadence": "manual", "timezone": routine.get("timezone", "Asia/Tokyo"),
            "source_types": ["public_web"], "search_queries": routine.get("initial_queries_proposed", [routine["objective"]]),
            "follow_accounts": routine.get("follow_accounts_from_seed", []),
            "exclusions": routine.get("exclusions", []), "max_candidates": 20,
            "review_policy": "human_review", "state": "draft", "enabled": False}
        if settings.get("project_id"):
            payload["project_id"] = settings["project_id"]
        add(key, MEDIA + "/research-routines", payload)
        if snapshot is None or not logical.startswith("research_"):
            continue
        platform = platforms[0]
        record_name = f"{platform}-benchmarks.json"
        evidence_name = f"{platform}-evidence.jsonl"
        if record_name not in snapshot["files"] or evidence_name not in snapshot["files"]:
            plan["blocked"].append({"key": "import:" + platform, "reason": "research_files_missing"})
            continue
        records = snapshot["files"][record_name]["value"]["records"]
        evidence = snapshot["files"][evidence_name]["value"]
        run_key = "import-run:" + platform
        add(run_key, MEDIA + "/research-runs", {"research_routine_id": ref(key),
            "routine_version": ref(key, "current_revision", "version"),
            "focus_note": "Imported public research checkpoint. Coverage remains incomplete; this is not a new model execution.",
            "source_refs": source_refs, "status": "partial",
            "omissions": ["Candidate/detail targets not achieved", "No claimed revenue/profit validation", "No model/provider execution asserted"]})
        for item in evidence:
            url = str(item.get("source_ref") or "")
            if not url.startswith("https://"):
                continue
            add("evidence:" + platform + ":" + item["evidence_id"],
                [MEDIA + "/research-runs/", ref(run_key), "/findings"], {
                    "kind": "signal", "statement": str(item.get("summary") or item["evidence_id"])[:8000],
                    "evidence": [{"type": "url", "url": url, "label": item["evidence_id"],
                       "note": canonical({"data_origin": item.get("data_origin"), "retrieved_at": item.get("retrieved_at"), "source_hash": item.get("source_hash"), "access_limitations": item.get("access_limitations")})[:2000]}]},
                [MEDIA + "/research-runs/", ref(run_key), "/findings?limit=100"], unwrap="find_by_id")
        for record in records:
            identity = record.get("identity") or {}
            url = str(identity.get("canonical_url") or "")
            candidate_evidence = research_record_evidence(record, platform=platform, snapshot=snapshot, documents=research_documents)
            if len(candidate_evidence) == 1 and url.startswith("https://"):
                candidate_evidence.append({"type": "url", "url": url, "label": "candidate URL", "note": "Discovery only; current identity/detail not verified by this import"})
            # Preserve the complete benchmark in Docs when a candidate summary
            # cannot hold it. Never truncate claims into fabricated certainty.
            summary = canonical(record)
            if len(summary) > 7900:
                projection = {k: record.get(k) for k in ("benchmark_id", "platform", "identity", "verification_state", "sampling", "monetization", "disposition")}
                projection["summary_projection"] = "source identity, sampling and money metadata only; complete original payload remains at the fixed source evidence reference; import does not verify execution, reuse rights or revenue"
                summary = canonical(projection)
            add("candidate:" + platform + ":" + record["benchmark_id"], MEDIA + "/research-candidates", {
                "research_run_id": ref(run_key), "candidate_key": digest([platform, record["benchmark_id"], snapshot["snapshot_hash"]]),
                "title": str(identity.get("display_name") or record["benchmark_id"])[:500], "summary": summary,
                "source_url": url if url.startswith("https://") else None,
                "evidence": candidate_evidence[:20], "reason": "Research checkpoint import; corroboration states and missingness are preserved. No financial success is asserted."})

    # A single bounded manual trigger can execute research without a model,
    # publication, or paid generation. It does not enable periodic scheduling.
    first = next((a for a in actions if a["key"] == "routine:research_youtube_benchmark"), None)
    if first:
        add("automation:bootstrap-research", MEDIA + "/automations", {
            "name": "MediaOps bootstrap public research", "project_id": settings.get("project_id"), "enabled": False,
            "execution_mode": "research_only", "trigger": {"type": "manual"},
            "discovery": {"type": "manual", "theme": "Original educational creator production process"},
            "research_binding": {"research_routine_id": ref(first["key"]), "routine_version": ref(first["key"], "current_revision", "version"), "fetch_pages": False},
            "planning_policy": {"candidate_count": 1, "novelty_policy": "conservative"}, "generation_action": {}, "fallback": {}},
            rollback={"endpoint": MEDIA + "/automations/{id}/enabled", "method": "PATCH", "payload": {"enabled": False}})
    plan["blocked"].extend([
        {"key": "image:Sunburst", "reason": "requested_model_route_requires_independent_verification"},
        {"key": "periodic", "reason": "requires_verified_owner_budget_pause_and_execution_limits"},
        {"key": "grants", "reason": "no_new_grants_or_access_expansion_performed"}])
    plan["plan_hash"] = digest({k: v for k, v in plan.items() if k != "plan_hash"})
    return plan


class Transport(Protocol):
    def request(self, method: str, path: str, payload: Any = None, key: str | None = None) -> Any: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BootstrapError("API redirects are not permitted")


class HttpTransport:
    def __init__(self, base_url: str, proxy_prefix: str = ""):
        url = urlsplit(base_url)
        if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise BootstrapError("invalid API base URL")
        if url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise BootstrapError("non-loopback API requires TLS")
        self.base_url = base_url.rstrip("/")
        if proxy_prefix and proxy_prefix != "/api/python-proxy":
            raise BootstrapError("unsupported frontend proxy prefix")
        self.proxy_prefix = proxy_prefix
        self.token = os.environ.get("AOITALK_API_TOKEN", "")
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, payload=None, key=None):
        if not path.startswith("/api/") or ".." in path or "://" in path:
            raise BootstrapError("invalid API path")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if key:
            headers["Idempotency-Key"] = key
        raw = None if payload is None else canonical(payload).encode()
        if raw is not None:
            headers["Content-Type"] = "application/json"
        target = path if path == "/api/auth/status" or not self.proxy_prefix else self.proxy_prefix + path
        request = Request(self.base_url + target, data=raw, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=90) as response:
                raw = response.read(MAX_INPUT_BYTES + 1)
                if len(raw) > MAX_INPUT_BYTES:
                    raise BootstrapError("API response exceeds maximum")
                return json.loads(raw)
        except HTTPError as exc:
            # Never include response bodies or authentication material in logs.
            raise ApiError(exc.code, path) from None
        except URLError:
            raise BootstrapError("API transport unavailable") from None


def path_value(value, results):
    resolved = resolve(value, results)
    return "".join(str(x) for x in resolved) if isinstance(resolved, list) else resolved


def unwrap(value, kind, entity_id=None):
    if kind == "find_by_id":
        return next((x for x in value if x.get("id") == entity_id), None)
    return value.get(kind) if kind else value


def stable_projection(value):
    """Hash authoritative row content, excluding live graph expansions."""
    if isinstance(value, list):
        return [stable_projection(x) for x in value]
    if isinstance(value, dict):
        return {k: stable_projection(v) for k, v in value.items()
                if k not in {"findings", "revisions", "route_warnings", "updated_at", "last_due_at", "next_due_at", "latest_run"}}
    return value


def match_fields(value, expected):
    return isinstance(value, dict) and all(value.get(k) == v for k, v in expected.items())


def normalized_source_ref(value):
    """Mirror wire normalization only; the service still validates URL safety."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.lower().startswith(("http://", "https://")):
        parsed = urlsplit(text)
        return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path, parsed.query, ""))
    return text


def evidence_projection(items):
    # Compare the submitted fields with the service's normalized ledger shape.
    result = []
    for item in items:
        kind = item.get("evidence_type", item.get("type"))
        value = {"type": kind, "label": (item.get("label") or "").strip() or None,
                 "note": (item.get("note") or "").strip() or None}
        if kind == "url":
            value["url"] = normalized_source_ref(item.get("source_url", item.get("url")))
        elif kind == "artifact":
            value["sha256"] = item.get("artifact_sha256", item.get("sha256"))
            value["mime_type"] = item.get("artifact_mime_type", item.get("mime_type"))
        else:
            raise BootstrapError("candidate evidence type changed")
        result.append(value)
    return result


def expected_candidate_content(payload, run):
    """Reconstruct the API's immutable candidate hash, including absent values."""
    normalized = []
    for ordinal, item in enumerate(evidence_projection(payload["evidence"]), 1):
        kind = item["type"]
        provenance = ({"type": "url", "url": item["url"]} if kind == "url" else
                      {"type": "artifact", "sha256": item["sha256"], "mime_type": item["mime_type"]})
        row = {"ordinal": ordinal, "evidence_type": kind, "label": item["label"], "note": item["note"],
               "source_url": item.get("url"), "artifact_sha256": item.get("sha256"),
               "artifact_mime_type": item.get("mime_type"), "provenance": provenance}
        row["evidence_hash"] = digest({k: row[k] for k in ("label", "note", "provenance")})
        normalized.append(row)
    date = payload.get("source_published_at")
    if date:
        date = datetime.fromisoformat(date.replace("Z", "+00:00"))
        if date.tzinfo is not None:
            date = date.astimezone(timezone.utc).replace(tzinfo=None)
        date = date.isoformat()
    else:
        date = None
    return {"research_routine_id": run.get("research_routine_id"), "research_run_id": run["id"],
            "routine_revision_id": run.get("research_routine_revision_id"),
            "candidate_key": payload["candidate_key"].strip(), "title": payload["title"].strip(),
            "summary": payload["summary"].strip(), "source_url": normalized_source_ref(payload.get("source_url")),
            "source_published_at": date,
            "relevance_score": None if payload.get("relevance_score") in (None, "") else float(payload["relevance_score"]),
            "freshness_score": None if payload.get("freshness_score") in (None, "") else float(payload["freshness_score"]),
            "evidence": normalized, "reason": (payload.get("reason") or "").strip() or None}


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA or plan.get("plan_hash") != digest({k: v for k, v in plan.items() if k != "plan_hash"}):
        raise BootstrapError("plan checksum mismatch")
    if plan.get("mode") != "research_only" or plan.get("model_substitution_allowed") is not False:
        raise BootstrapError("bootstrap supports research_only without model substitution")
    keys = set()
    for action in plan.get("actions", []):
        if action["key"] in keys or action.get("payload_hash") != digest(action["payload"]):
            raise BootstrapError("duplicate action or payload checksum mismatch")
        keys.add(action["key"])
        endpoint = action["endpoint"]
        allowed = endpoint in {"/api/docs/nodes", "/api/agents", MEDIA + "/research-routines",
                               MEDIA + "/research-runs", MEDIA + "/research-candidates", MEDIA + "/automations"} if isinstance(endpoint, str) else (
            len(endpoint) == 3 and isinstance(endpoint[1], dict) and "$ref" in endpoint[1]
            and (endpoint[0], endpoint[2]) in {("/api/agents/", "/revisions"), (MEDIA + "/research-runs/", "/findings")})
        if not allowed:
            raise BootstrapError("bootstrap endpoint is outside the allowed scope")
        if isinstance(endpoint, list):
            expected_read = "/api/agent-revisions/{id}" if endpoint[0] == "/api/agents/" else [endpoint[0], endpoint[1], "/findings?limit=100"]
        else:
            expected_read = endpoint + "/{id}"
        if action.get("read_path") != expected_read:
            raise BootstrapError("readback endpoint is outside the allowed scope")
        reuse = action.get("reuse")
        if reuse:
            expected_reuse = "/api/agents?limit=200" if endpoint == "/api/agents" else f"/api/docs/nodes/{action['payload'].get('id')}" if endpoint == "/api/docs/nodes" else None
            if not expected_reuse or reuse.get("path") != expected_reuse:
                raise BootstrapError("reuse endpoint is outside the allowed scope")
        payload = action["payload"]
        if endpoint == "/api/docs/nodes":
            body = payload.get("body_json")
            if not isinstance(body, dict) or (body and (
                body.get("format") != "doc_block" or body.get("block_type") != "code"
                or not isinstance(body.get("content"), str) or not isinstance(body.get("label"), str)
                or not payload.get("parent_id")
            )):
                raise BootstrapError("Docs source requires a typed child block with string content and label")
        if action["key"].startswith("revision:"):
            role = action["key"].split(":", 1)[1]
            binding = (plan.get("role_bindings") or {}).get(role)
            expected_endpoint = ["/api/agents/", ref("agent:" + role), "/revisions"]
            if not isinstance(binding, dict) or endpoint != expected_endpoint:
                raise BootstrapError("revision requires its exact role binding")
            for field in ("agent_team_id", "execution_profile_id", "allowed_subagent_ids"):
                if payload.get(field, [] if field == "allowed_subagent_ids" else None) != binding.get(field, [] if field == "allowed_subagent_ids" else None):
                    raise BootstrapError("revision differs from the inspected model binding")
        if payload.get("enabled") is True or payload.get("state") not in {None, "draft"}:
            raise BootstrapError("bootstrap cannot enable recurring work or activate identities")
        if isinstance(endpoint, str) and endpoint.endswith("/automations"):
            if payload.get("execution_mode") != "research_only" or payload.get("generation_action"):
                raise BootstrapError("bootstrap cannot submit generation or publication")
        if any(k in payload for k in ("actor_user_id", "owner_user_id", "role", "permissions", "authorization")):
            raise BootstrapError("bootstrap cannot supply an authority principal or grant")


class Runner:
    def __init__(self, transport: Transport, plan: dict[str, Any], journal: Path):
        validate_plan(plan)
        self.transport, self.plan, self.journal = transport, plan, journal
        self.state = read_json(journal) if journal.exists() else {"schema": SCHEMA, "scope_key": plan["scope_key"], "bindings": {}, "live_runs": {}}
        if self.state["scope_key"] != plan["scope_key"]:
            raise BootstrapError("journal scope mismatch")
        self.results: dict[str, Any] = {}

    def persist(self):
        save_json(self.journal, self.state)

    def readback_projection(self, action, value):
        projected = stable_projection(value)
        if action["endpoint"] == MEDIA + "/research-runs" and action["key"].startswith("import-run:"):
            # Hash the original refs, not the service's live candidate expansion.
            # This also accepts the existing creation-time journal hashes.
            projected["source_refs"] = self.initial_source_refs(action)
        return projected

    def initial_source_refs(self, action):
        refs = resolve(action["payload"], self.results).get("source_refs") or []
        return list(dict.fromkeys(normalized_source_ref(x) for x in refs))

    def verify_run_source_refs(self, action, run):
        """Permit only exact planned candidates already durably saved to this run.

        Includes POST-committed candidates not yet in the journal, so resume does
        not mistake a crash after commit for source tampering. No list order is
        used: the serial import plan defines the service append order.
        """
        expected = self.initial_source_refs(action)
        candidates = [a for a in self.plan["actions"] if a["endpoint"] == MEDIA + "/research-candidates"
                      and a["payload"].get("research_run_id") == ref(action["key"])]
        if candidates:
            planned = {a["payload"]["candidate_key"]: a for a in candidates}
            if len(planned) != len(candidates) or len(candidates) > 5000:
                raise BootstrapError("import candidate verification window is invalid")
            saved = {}
            for offset in range(0, 5001, 100):
                query = urlencode({"research_run_id": run["id"], "limit": 100, "offset": offset})
                rows = self.transport.request("GET", MEDIA + "/research-candidates?" + query)
                if not isinstance(rows, list) or len(rows) > 100:
                    raise BootstrapError("candidate readback page is invalid")
                for row in rows:
                    key = row.get("candidate_key")
                    candidate_action = planned.get(key)
                    if not candidate_action or key in saved:
                        raise BootstrapError("unplanned or duplicate saved import candidate")
                    payload = resolve(candidate_action["payload"], {**self.results, action["key"]: run})
                    content = expected_candidate_content(payload, run)
                    if (row.get("research_run_id") != run["id"]
                        or row.get("owner_user_id") != self.plan["expected_owner_user_id"]
                        or row.get("project_id") != run.get("project_id")
                        or any(row.get(field) != content[field] for field in (
                            "research_routine_id", "routine_revision_id", "source_url", "source_published_at",
                            "relevance_score", "freshness_score", "title", "summary", "reason"))
                        or row.get("candidate_hash") != digest(content)
                        or evidence_projection(row.get("evidence") or []) != evidence_projection(payload["evidence"])):
                        raise BootstrapError("saved import candidate source or scope changed")
                    binding = self.state["bindings"].get(candidate_action["key"])
                    if not binding and (row.get("status") not in {"discovered", "expired"}
                                        or row.get("decision_version", 0) != 0 or row.get("content_item_id")
                                        or row.get("created_by") != self.plan["expected_owner_user_id"]):
                        raise BootstrapError("unrecorded import candidate decision changed")
                    if binding and (binding["id"] != row.get("id") or binding["payload_hash"] != candidate_action["payload_hash"]
                                    or binding["readback_hash"] != digest(stable_projection(row))):
                        raise BootstrapError("saved import candidate journal changed")
                    saved[key] = row
                if len(rows) < 100:
                    break
            else:
                raise BootstrapError("candidate verification pagination exceeds limit")
            for candidate in candidates:
                row = saved.get(candidate["payload"]["candidate_key"])
                if row and row.get("source_url") and row["source_url"] not in expected and len(expected) < 100:
                    expected.append(row["source_url"])
        if run.get("source_refs") != expected:
            raise BootstrapError("import run source refs differ from saved planned candidates")

    def get_action(self, action, entity_id):
        path = path_value(action["read_path"], self.results).replace("{id}", entity_id)
        if action.get("unwrap") == "find_by_id":
            value = None
            for offset in range(0, 6001, 100):
                result = self.transport.request("GET", path + f"&offset={offset}")
                value = unwrap(result, "find_by_id", entity_id)
                if value is not None or len(result) < 100:
                    break
        else:
            result = self.transport.request("GET", path)
            value = unwrap(result, action.get("unwrap"), entity_id)
        if not value or str(value.get("id")) != entity_id:
            raise BootstrapError("readback identity mismatch")
        if action["endpoint"] == "/api/docs/nodes":
            expected = resolve(action["payload"], self.results)
            fields = {k: expected[k] for k in ("id", "title", "parent_id", "node_type", "body_json") if k in expected}
            if not match_fields(value, fields):
                raise BootstrapError("Docs canonical content or parent changed")
        owner = value.get("owner_user_id")
        if owner and owner != self.plan["expected_owner_user_id"]:
            raise BootstrapError("readback owner mismatch")
        if action["endpoint"] == MEDIA + "/research-runs" and action["key"].startswith("import-run:"):
            self.verify_run_source_refs(action, value)
        return value

    def reuse(self, action):
        spec = action.get("reuse")
        if not spec:
            return None
        try:
            value = self.transport.request("GET", spec["path"])
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise
        if "list_match" in spec:
            value = unwrap(value, spec.get("unwrap"))
            candidates = [x for x in value if (x.get("id") == spec["explicit_id"] if spec.get("explicit_id") else match_fields(x, spec["list_match"]))]
            if len(candidates) > 1:
                raise BootstrapError("ambiguous existing entity; explicit binding required")
            if not candidates:
                return None
            selected = candidates[0]
            if selected.get("created_by") != self.plan["expected_owner_user_id"]:
                raise BootstrapError("existing Agent creator differs; no revision added")
            if selected.get("state") != "draft" and not spec.get("explicit_id"):
                raise BootstrapError("active existing Agent requires an explicit inspected binding")
            return selected
        value = unwrap(value, spec.get("unwrap"))
        if not match_fields(value, spec["match"]):
            raise BootstrapError("existing source differs; no overwrite performed")
        return value

    def verify_owner(self):
        status = self.transport.request("GET", "/api/auth/status")
        user = status.get("user") or {}
        if not status.get("authenticated") or user.get("id") != self.plan["expected_owner_user_id"]:
            raise BootstrapError("authenticated owner cannot be verified; use the authenticated frontend API")
        if user.get("password_reset_required"):
            raise BootstrapError("account requires password reset")

    def verify_catalog(self):
        bindings = self.plan.get("role_bindings") or {}
        if not bindings:
            return
        catalog = self.transport.request("GET", "/api/agents/catalog")
        teams = {t["team_id"]: t for t in catalog.get("teams", [])}
        for role, binding in bindings.items():
            team = teams.get(binding["agent_team_id"])
            if not team:
                raise BootstrapError(f"catalog team unavailable: {role}")
            profile = next((p for p in team.get("execution_profiles", []) if p["profile_id"] == binding["execution_profile_id"]), None)
            if not profile:
                raise BootstrapError(f"catalog profile unavailable: {role}")
            members = binding.get("allowed_subagent_ids") or []
            routes = [r for r in profile.get("routes", []) if r.get("subagent_id") in members]
            if not members or len(routes) != len(members):
                raise BootstrapError(f"executable subagent binding required: {role}")
            if any(r.get("provider") != binding["resolved_provider"] or r.get("model") != binding["resolved_model_id"] for r in routes):
                raise BootstrapError(f"model route changed or unresolved: {role}")

    def run(self, *, apply=False, readback_only=False):
        self.verify_owner()
        if apply:
            self.verify_catalog()
        reports = []
        for action in self.plan["actions"]:
            key = action["key"]
            previous = self.state["bindings"].get(key)
            if previous:
                if previous["payload_hash"] != action["payload_hash"]:
                    raise BootstrapError(f"planned content changed: {key}")
                current = self.get_action(action, previous["id"])
                if previous.get("readback_hash") != digest(self.readback_projection(action, current)):
                    raise BootstrapError(f"remote content changed: {key}")
                self.results[key] = current
                reports.append({"key": key, "status": "verified", "id": current["id"]})
                continue
            if readback_only:
                reports.append({"key": key, "status": "not_applied"})
                continue
            pending = self.state.get("pending")
            if pending and pending.get("key") == key and (pending.get("idempotency_key") != action["idempotency_key"] or pending.get("payload_hash") != action["payload_hash"]):
                raise BootstrapError("unresolved registration content changed")
            existing = self.reuse(action)
            if existing:
                self.results[key] = existing
                reports.append({"key": key, "status": "reuse", "id": existing["id"]})
            elif not apply:
                # Placeholder IDs only exist in the dry-run output, never in DB.
                self.results[key] = {"id": "PLAN_ONLY", "current_revision": {"version": 1}}
                reports.append({"key": key, "status": "create"})
                continue
            else:
                pending = self.state.get("pending")
                expected = {"key": key, "idempotency_key": action["idempotency_key"], "payload_hash": action["payload_hash"]}
                if pending and any(pending.get(k) != v for k, v in expected.items()):
                    raise BootstrapError("unresolved registration cannot change before reconciliation")
                if pending and pending.get("id"):
                    existing = self.get_action(action, pending["id"])
                else:
                    self.state["pending"] = expected
                    self.persist()
                    endpoint = path_value(action["endpoint"], self.results)
                    payload = resolve(action["payload"], self.results)
                    result = self.transport.request("POST", endpoint, payload, action["idempotency_key"])
                    existing = unwrap(result, action.get("unwrap") if action.get("unwrap") != "find_by_id" else None)
                    if not existing or not existing.get("id"):
                        raise BootstrapError("creation response has no id")
                    self.state["pending"]["id"] = existing["id"]
                    self.persist()
                self.results[key] = existing
                reports.append({"key": key, "status": "applied", "id": existing["id"]})
            if apply:
                current = self.get_action(action, str(existing["id"]))
                self.results[key] = current
                self.state["bindings"][key] = {"id": current["id"], "payload_hash": action["payload_hash"],
                    "readback_hash": digest(self.readback_projection(action, current)), "created": reports[-1]["status"] == "applied"}
                self.state["pending"] = None
                self.persist()
        return {"schema": SCHEMA, "plan_hash": self.plan["plan_hash"], "results": reports, "blocked": self.plan["blocked"]}

    def rollback(self):
        self.verify_owner()
        changes = []
        for action in reversed(self.plan["actions"]):
            binding = self.state["bindings"].get(action["key"])
            agent = action["key"].startswith("agent:")
            automation = action["key"] == "automation:bootstrap-research"
            if not binding or not binding["created"] or not (agent or automation):
                continue
            current = self.get_action(action, binding["id"])
            if (agent and current.get("state") != "active") or (automation and current.get("enabled") is False):
                continue
            if digest(stable_projection(current)) != binding["readback_hash"]:
                raise BootstrapError("changed registration is not automatically paused")
            path = f"/api/agents/{binding['id']}/state" if agent else f"{MEDIA}/automations/{binding['id']}/enabled"
            payload = {"state": "paused", "expected_state": "active"} if agent else {"enabled": False}
            self.transport.request("PATCH", path, payload)
            readback = self.get_action(action, binding["id"])
            binding["readback_hash"] = digest(stable_projection(readback))
            changes.append(action["key"])
            self.persist()
        return {"paused": changes, "retained": "all evidence and revisions; no deletion"}

    def initial_run(self):
        self.verify_owner()
        key = "automation:bootstrap-research"
        binding = self.state["bindings"].get(key)
        if not binding:
            raise BootstrapError("bootstrap automation must be applied first")
        action = next(a for a in self.plan["actions"] if a["key"] == key)
        # Ensure exact approved research-only revision and no automatic publishing.
        current = self.transport.request("GET", MEDIA + "/automations/" + binding["id"])
        if current["current_revision"]["execution_mode"] != "research_only":
            raise BootstrapError("live run is restricted to research_only")
        if digest(stable_projection(current)) != binding["readback_hash"]:
            raise BootstrapError("automation changed after bootstrap")
        trigger = "mediaops-bootstrap:" + action["payload_hash"][:40]
        result = self.transport.request("POST", MEDIA + "/automations/" + binding["id"] + "/runs",
                                        {"trigger_key": trigger, "trigger_kind": "manual", "execute": True})
        actual = self.transport.request("GET", MEDIA + "/automations/runs/" + result["id"])
        if actual["id"] != result["id"]:
            raise BootstrapError("run readback mismatch")
        self.state["live_runs"][trigger] = {"id": actual["id"], "state": actual["state"], "readback_hash": digest(actual)}
        self.persist()
        return {"id": actual["id"], "state": actual["state"], "research_run_id": actual.get("research_run_id"), "mode": "research_only"}


class JournalLock:
    """OS-released lock: a process crash cannot leave an active lease behind."""
    def __init__(self, path: Path):
        self.path = path.with_suffix(path.suffix + ".lock")
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise BootstrapError("another bootstrap process owns this journal") from None
        return self

    def __exit__(self, *args):
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_UN)
        finally:
            self.handle.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "plan", "diff", "apply", "readback", "resume", "rollback", "initial-run"])
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--research", type=Path)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--proxy-prefix", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            data = validate_bundle(args.bundle)
            result = {"valid": True, "sources": len(data["sources"]), "roles": len(data["roles"]), "routines": len(data["routines"])}
        elif args.command == "plan":
            result = make_plan(args.bundle, args.research, read_json(args.settings))
        else:
            if not args.base_url or not args.plan or not args.journal:
                raise BootstrapError("--base-url, --plan and --journal are required")
            with JournalLock(args.journal):
                runner = Runner(HttpTransport(args.base_url, args.proxy_prefix), read_plan(args.plan), args.journal)
                if args.command == "rollback": result = runner.rollback()
                elif args.command == "initial-run": result = runner.initial_run()
                else: result = runner.run(apply=args.command in {"apply", "resume"}, readback_only=args.command == "readback")
        if args.output:
            save_json(args.output, result)
        print(canonical({"ok": True, "command": args.command, "output_written": bool(args.output),
                         "count": len(result.get("results", result.get("actions", []))), "state": result.get("state")}))
        return 0
    except (BootstrapError, KeyError, TypeError, ValueError, OSError) as exc:
        print(canonical({"ok": False, "error_type": type(exc).__name__, "message": str(exc) if isinstance(exc, BootstrapError) else "invalid input or unavailable file"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
