"""Prepare or explicitly apply the fixed CrowdWorks Muse role revision.

The command is read-only by default. It reads one exact AgentRevision,
projects the Muse route into the P2 model_overrides and delegation fields,
and, on an explicit apply, submits one idempotent revision POST. The retired
topology configuration is no longer a runtime input or a write target.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import UUID


AGENT_ID = "bc67fa3e-5b2a-4f26-aea6-298ff584cb34"
AGENT_SLUG = "crowdworks-app-developer"
BASE_REVISION_ID = "969107da-d715-480d-b979-e771985f3964"
BASE_REVISION_VERSION = 3
CAPABILITY_ID = "remote_agent_delegate"
REMOTE_ROUTE = {"kind": "llm", "provider": "muse-remote", "model": "muse-chat"}
IDEMPOTENCY_KEY = "muse-appdev-remote-enable-20261023-01"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_COLLECTION_ITEMS = 200
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class OperationError(Exception):
    """Safe failure result with no remote response body or credential values."""

    def __init__(
        self,
        code: str,
        *,
        stage: str,
        http_status: int | None = None,
        submission_uncertain: bool = False,
        blocking_revisions: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.http_status = http_status
        self.submission_uncertain = submission_uncertain
        self.blocking_revisions = blocking_revisions or []

    def public_result(self, *, mode: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "mode": mode,
            "error": self.code,
            "stage": self.stage,
            "submission_uncertain": self.submission_uncertain,
        }
        if self.http_status is not None:
            result["http_status"] = self.http_status
        if self.blocking_revisions:
            result["blocking_revisions"] = self.blocking_revisions
        if self.submission_uncertain and self.stage.startswith("revision_post"):
            result["idempotency_key"] = IDEMPOTENCY_KEY
            result["next_step"] = (
                "Reconcile the existing revision before another apply; keep this exact key and never replace it."
            )
        return result


def revision_sha256(revision: dict[str, Any]) -> str:
    """Hash the complete bounded revision response for the apply fence."""

    raw = json.dumps(
        revision,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# Kept as a source-compatible name for callers that only used this helper as
# a generic JSON hash. It no longer hashes a Team/config graph.
graph_sha256 = revision_sha256


def _mapping(value: Any, *, stage: str, code: str = "invalid_response") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OperationError(code, stage=stage)
    return value


def _uuid(value: Any) -> str | None:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _task_error_for_status(status: int, *, stage: str) -> None:
    """Raise the canonical task adapter error so its auth owner handles 401/429."""

    code = {
        400: "invalid_input",
        401: "authentication_failed",
        403: "permission_denied",
        404: "not_found",
        409: "conflict",
        422: "invalid_input",
        429: "rate_limited",
    }.get(status, "server_error")
    try:
        module = _load_plugin_tasks_module()
        raise module.TaskError(code, status)
    except OperationError:
        raise
    except Exception as exc:
        if getattr(exc, "code", None) == code and getattr(exc, "http_status", None) == status:
            raise
        raise OperationError(code, stage=stage, http_status=status) from None


async def _request_json(
    client: Any,
    method: str,
    path: str,
    *,
    expected_status: int,
    stage: str,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if params is not None:
        kwargs["params"] = params
    if payload is not None:
        kwargs["json"] = payload
    if headers is not None:
        kwargs["headers"] = headers

    status: int | None = None
    try:
        async with client.http.stream(method, path, **kwargs) as response:
            status = int(response.status_code)
            if status != expected_status:
                if method == "POST" and status >= 500:
                    raise OperationError(
                        "revision_submission_uncertain",
                        stage=stage,
                        http_status=status,
                        submission_uncertain=True,
                    )
                _task_error_for_status(status, stage=stage)
            parts: list[bytes] = []
            size = 0
            async for part in response.aiter_bytes():
                size += len(part)
                if size > MAX_RESPONSE_BYTES:
                    if method == "POST":
                        raise OperationError(
                            "revision_submission_uncertain",
                            stage=stage,
                            submission_uncertain=True,
                        )
                    raise OperationError("invalid_response", stage=stage)
                parts.append(part)
    except OperationError:
        raise
    except Exception as exc:
        if status is not None and getattr(exc, "http_status", None) == status and getattr(exc, "code", None):
            raise
        if method == "POST":
            raise OperationError(
                "revision_submission_uncertain",
                stage=stage,
                submission_uncertain=True,
            ) from None
        raise OperationError("connection_error", stage=stage) from None

    try:
        value = json.loads(b"".join(parts))
    except (ValueError, UnicodeError):
        if method == "POST":
            raise OperationError(
                "revision_submission_uncertain",
                stage=stage,
                submission_uncertain=True,
            ) from None
        raise OperationError("invalid_response", stage=stage) from None
    if not isinstance(value, dict):
        if method == "POST":
            raise OperationError(
                "revision_submission_uncertain",
                stage=stage,
                submission_uncertain=True,
            )
        raise OperationError("invalid_response", stage=stage)
    return value


async def _get_base_revision(client: Any) -> dict[str, Any]:
    envelope = await _request_json(
        client,
        "GET",
        f"/api/agent-revisions/{BASE_REVISION_ID}",
        expected_status=200,
        stage="base_revision_get",
    )
    revision = _mapping(envelope.get("revision"), stage="base_revision_get")
    if (
        revision.get("id") != BASE_REVISION_ID
        or _uuid(revision.get("agent_id")) != AGENT_ID
        or revision.get("version") != BASE_REVISION_VERSION
    ):
        raise OperationError("base_revision_mismatch", stage="base_revision_get")
    for field in ("model_overrides", "tool_ids", "delegate_policy"):
        if field not in revision or revision[field] is None:
            raise OperationError("base_revision_v2_missing", stage="base_revision_get")
    ceiling = revision.get("capability_ceiling")
    if not isinstance(ceiling, list) or any(not isinstance(item, str) for item in ceiling):
        raise OperationError("base_revision_capability_mismatch", stage="base_revision_get")
    if not isinstance(revision.get("model_overrides"), dict):
        raise OperationError("base_revision_model_overrides_invalid", stage="base_revision_get")
    if not isinstance(revision.get("tool_ids"), list) or any(
        not isinstance(item, str) for item in revision["tool_ids"]
    ):
        raise OperationError("base_revision_tool_ids_invalid", stage="base_revision_get")
    if not isinstance(revision.get("delegate_policy"), dict):
        raise OperationError("base_revision_delegate_policy_invalid", stage="base_revision_get")
    return revision


def _route_override(base_revision: dict[str, Any]) -> dict[str, Any]:
    overrides = copy.deepcopy(base_revision["model_overrides"])
    existing = overrides.get("code")
    block: dict[str, Any] = {"routes": [copy.deepcopy(REMOTE_ROUTE)]}
    if isinstance(existing, dict) and isinstance(existing.get("allow_paid_fallback"), bool):
        block["allow_paid_fallback"] = existing["allow_paid_fallback"]
    overrides["code"] = block
    return overrides


def _revision_post_body(base_revision: dict[str, Any]) -> dict[str, Any]:
    ceiling = list(dict.fromkeys([*base_revision["capability_ceiling"], CAPABILITY_ID]))
    tools = list(dict.fromkeys([
        *base_revision["tool_ids"],
        CAPABILITY_ID,
        "delegate_worker:muse",
    ]))
    policy = copy.deepcopy(base_revision["delegate_policy"])
    if set(policy) != {"agents", "workers"}:
        raise OperationError("base_revision_delegate_policy_invalid", stage="base_revision_get")
    workers = policy.get("workers")
    if not isinstance(workers, list) or any(not isinstance(item, str) for item in workers):
        raise OperationError("base_revision_delegate_policy_invalid", stage="base_revision_get")
    policy["workers"] = list(dict.fromkeys([*workers, "muse"]))
    return {
        "display_name": base_revision.get("display_name"),
        "mission": base_revision.get("mission"),
        "responsibility_summary": base_revision.get("responsibility_summary"),
        "operational_instructions": base_revision.get("operational_instructions"),
        "capability_ceiling": ceiling,
        "wake_policy": copy.deepcopy(base_revision.get("wake_policy")),
        "budget_policy": copy.deepcopy(base_revision.get("budget_policy")),
        "concurrency_policy": copy.deepcopy(base_revision.get("concurrency_policy")),
        "model_overrides": _route_override(base_revision),
        "tool_ids": tools,
        "delegate_policy": policy,
        "idempotency_key": IDEMPOTENCY_KEY,
    }


def _revision_has_muse_authority(revision: dict[str, Any]) -> bool:
    ceiling = revision.get("capability_ceiling")
    tools = revision.get("tool_ids")
    policy = revision.get("delegate_policy")
    if ceiling is not None and (
        not isinstance(ceiling, list) or any(not isinstance(item, str) for item in ceiling)
    ):
        raise OperationError("invalid_response", stage="revision_scan")
    if tools is not None and (
        not isinstance(tools, list) or any(not isinstance(item, str) for item in tools)
    ):
        raise OperationError("invalid_response", stage="revision_scan")
    if policy is not None and not isinstance(policy, dict):
        raise OperationError("invalid_response", stage="revision_scan")
    return bool(
        CAPABILITY_ID in (ceiling or [])
        or CAPABILITY_ID in (tools or [])
        or "delegate_worker:muse" in (tools or [])
        or "muse" in ((policy or {}).get("workers") or [])
    )


async def _scan_agent_revisions(client: Any) -> list[dict[str, Any]]:
    """Read all revisions and report existing revision-local Muse authority."""

    agents_envelope = await _request_json(
        client,
        "GET",
        "/api/agents",
        expected_status=200,
        stage="agent_revision_scan",
        params={"limit": MAX_COLLECTION_ITEMS},
    )
    agents = agents_envelope.get("agents")
    total = agents_envelope.get("total")
    if not isinstance(agents, list) or not isinstance(total, int):
        raise OperationError("invalid_agent_list", stage="agent_revision_scan")
    if len(agents) != total or len(agents) >= MAX_COLLECTION_ITEMS:
        raise OperationError("agent_revision_scan_incomplete", stage="agent_revision_scan")
    target_seen = False
    conflicts: list[dict[str, Any]] = []
    for agent in agents:
        if not isinstance(agent, dict):
            raise OperationError("invalid_agent_list", stage="agent_revision_scan")
        agent_id = _uuid(agent.get("id"))
        if agent_id is None:
            raise OperationError("invalid_agent_list", stage="agent_revision_scan")
        if agent_id == AGENT_ID:
            if agent.get("slug") != AGENT_SLUG:
                raise OperationError("target_agent_slug_mismatch", stage="agent_revision_scan")
            target_seen = True
        envelope = await _request_json(
            client,
            "GET",
            f"/api/agents/{agent_id}/revisions",
            expected_status=200,
            stage="agent_revision_scan",
            params={"limit": MAX_COLLECTION_ITEMS},
        )
        revisions = envelope.get("revisions")
        revision_total = envelope.get("total")
        if not isinstance(revisions, list) or not isinstance(revision_total, int):
            raise OperationError("invalid_revision_list", stage="agent_revision_scan")
        if len(revisions) != revision_total or len(revisions) >= MAX_COLLECTION_ITEMS:
            raise OperationError("agent_revision_scan_incomplete", stage="agent_revision_scan")
        for revision in revisions:
            if not isinstance(revision, dict):
                raise OperationError("invalid_revision_list", stage="agent_revision_scan")
            if (
                agent_id != AGENT_ID
                and _revision_has_muse_authority(revision)
                and revision.get("state") not in {"retired", "archived"}
            ):
                conflicts.append({
                    "agent_id": agent_id,
                    "agent_revision_id": str(revision.get("id") or ""),
                    "version": revision.get("version"),
                })
    if not target_seen:
        raise OperationError("target_agent_missing", stage="agent_revision_scan")
    return conflicts


def _verify_revision_response(response: dict[str, Any], *, body: dict[str, Any]) -> dict[str, Any]:
    revision = _mapping(response.get("revision"), stage="revision_post_response")
    if (
        _uuid(revision.get("agent_id")) != AGENT_ID
        or any(revision.get(field) != body.get(field) for field in body if field != "idempotency_key")
        or revision.get("model_overrides") is None
        or revision.get("tool_ids") is None
        or revision.get("delegate_policy") is None
    ):
        raise OperationError(
            "revision_response_did_not_match_request",
            stage="revision_post",
            submission_uncertain=True,
        )
    return {"agent_revision_id": revision.get("id"), "version": revision.get("version")}


async def _apply_with_client(client: Any, *, expected_revision_sha256: str) -> dict[str, Any]:
    base_revision = await _get_base_revision(client)
    before_hash = revision_sha256(base_revision)
    if before_hash != expected_revision_sha256:
        raise OperationError("revision_hash_conflict", stage="initial_revision_check")
    post_body = _revision_post_body(base_revision)
    conflicts = await _scan_agent_revisions(client)
    fresh_base = await _get_base_revision(client)
    if revision_sha256(fresh_base) != before_hash:
        raise OperationError("revision_hash_conflict", stage="fresh_revision_check")
    post_response = await _request_json(
        client,
        "POST",
        f"/api/agents/{AGENT_ID}/revisions",
        expected_status=201,
        stage="revision_post",
        payload=post_body,
        headers={"Idempotency-Key": IDEMPOTENCY_KEY},
    )
    posted = _verify_revision_response(post_response, body=post_body)
    return {
        "ok": True,
        "mode": "apply",
        "agent_id": AGENT_ID,
        "agent_slug": AGENT_SLUG,
        "base_revision_id": BASE_REVISION_ID,
        "base_revision_sha256": before_hash,
        "route": copy.deepcopy(REMOTE_ROUTE),
        "capability_id": CAPABILITY_ID,
        "revision_post_attempted": True,
        "idempotency_key": IDEMPOTENCY_KEY,
        "other_muse_revisions": conflicts,
        **posted,
    }


async def _prepare_with_client(client: Any) -> dict[str, Any]:
    base_revision = await _get_base_revision(client)
    before_hash = revision_sha256(base_revision)
    post_body = _revision_post_body(base_revision)
    conflicts = await _scan_agent_revisions(client)
    return {
        "ok": True,
        "mode": "prepare",
        "agent_id": AGENT_ID,
        "agent_slug": AGENT_SLUG,
        "base_revision_id": BASE_REVISION_ID,
        "base_revision_version": base_revision.get("version"),
        "base_revision_sha256": before_hash,
        "route": copy.deepcopy(REMOTE_ROUTE),
        "capability_id": CAPABILITY_ID,
        "revision_capability_added": CAPABILITY_ID not in base_revision.get("capability_ceiling", []),
        "revision_tool_added": "delegate_worker:muse" not in base_revision.get("tool_ids", []),
        "revision_fields": [
            "display_name",
            "mission",
            "responsibility_summary",
            "operational_instructions",
            "capability_ceiling",
            "wake_policy",
            "budget_policy",
            "concurrency_policy",
            "model_overrides",
            "tool_ids",
            "delegate_policy",
        ],
        "other_muse_revisions": conflicts,
        "revision_post_would_be_needed": True,
        "write_attempted": False,
        "apply_invocation": (
            "python scripts/prepare_crowdworks_muse_remote_revision.py --apply "
            f"--expected-revision-sha256 {before_hash}"
        ),
        "proposed_revision": post_body,
    }


async def _canonical_session(
    callback: Callable[[Any], Awaitable[dict[str, Any]]],
) -> Any:
    module = _load_plugin_tasks_module()
    return await module._session(callback)


def _load_plugin_tasks_module() -> Any:
    plugin_root = Path(__file__).resolve().parents[1] / "plugins" / "AoiTalk_Plugin"
    plugin_root_text = str(plugin_root)
    if plugin_root_text not in sys.path:
        sys.path.insert(0, plugin_root_text)
    from aoitalk_plugin import aoitalk_tasks

    return aoitalk_tasks


async def run_helper(
    *,
    apply: bool = False,
    expected_revision_sha256: str | None = None,
    expected_graph_sha256: str | None = None,
    session_runner: Callable[[Callable[[Any], Awaitable[dict[str, Any]]]], Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """Run one prepare or apply pass using an injectable authenticated session."""

    # Accept the old argument name only as an opaque hash compatibility alias.
    if expected_revision_sha256 is None:
        expected_revision_sha256 = expected_graph_sha256
    if apply:
        if not isinstance(expected_revision_sha256, str) or not SHA256_RE.fullmatch(expected_revision_sha256):
            raise OperationError("expected_revision_sha256_required", stage="arguments")
    elif expected_revision_sha256 is not None:
        raise OperationError("unexpected_revision_sha256", stage="arguments")

    runner = session_runner or _canonical_session
    result: dict[str, Any] = {}

    async def callback(client: Any) -> dict[str, Any]:
        nonlocal result
        if apply:
            assert expected_revision_sha256 is not None
            result = await _apply_with_client(client, expected_revision_sha256=expected_revision_sha256)
        else:
            result = await _prepare_with_client(client)
        return {"ok": True, "status": "completed"}

    await runner(callback)
    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform one idempotent Agent revision POST")
    parser.add_argument("--expected-revision-sha256")
    parser.add_argument("--expected-graph-sha256", dest="expected_revision_sha256", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    try:
        result = await run_helper(
            apply=args.apply,
            expected_revision_sha256=args.expected_revision_sha256,
        )
    except OperationError as exc:
        print(json.dumps(exc.public_result(mode="apply" if args.apply else "prepare"), ensure_ascii=False))
        return 2
    except Exception as exc:
        code = getattr(exc, "code", "operation_failed")
        status = getattr(exc, "http_status", None)
        safe: dict[str, Any] = {
            "ok": False,
            "mode": "apply" if args.apply else "prepare",
            "error": code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "operation_failed",
        }
        if isinstance(status, int):
            safe["http_status"] = status
        print(json.dumps(safe, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main_async(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
