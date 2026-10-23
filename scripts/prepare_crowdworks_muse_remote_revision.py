"""Prepare or explicitly apply the fixed CrowdWorks Muse remote Agent change.

Default invocation is read-only. For an apply, first review the emitted graph
SHA-256, then pass it with ``--apply --expected-graph-sha256``. All HTTP work
uses the AoiTalk plugin's canonical ``aoitalk_tasks._session`` authentication
helper. The only mutations are the Agent Team config PUT and one revision POST.
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
TEAM_ID = "app_development"
DEFAULT_EXECUTION_PROFILE_ID = "crowdworks-codex-61"
ALLOWED_SUBAGENT_IDS = [
    "code_explorer",
    "architecture_planner",
    "code_implementer",
    "code_reviewer",
]
SUBAGENT_ID = "code_implementer"
CAPABILITY_ID = "remote_agent_delegate"
RETAINED_SUBAGENT_CAPABILITIES = {
    "workspace_read",
    "workspace_write",
    "command_execute",
}
IDEMPOTENCY_KEY = "muse-appdev-remote-enable-20261023-01"
EXECUTION_PROFILE = {
    "profile_id": "appdev-muse-remote",
    "name": "Muse remote (explicit)",
    "enabled": True,
    "default_route": {
        "inherit_model": False,
        "provider": "muse-remote",
        "backend": "remote",
    },
    "overrides": {},
}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_COLLECTION_ITEMS = 200
GRAPH_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


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
        config_write_succeeded: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.http_status = http_status
        self.submission_uncertain = submission_uncertain
        self.blocking_revisions = blocking_revisions or []
        self.config_write_succeeded = config_write_succeeded

    def public_result(self, *, mode: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "mode": mode,
            "error": self.code,
            "stage": self.stage,
            "submission_uncertain": self.submission_uncertain,
            "config_write_succeeded": self.config_write_succeeded,
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


def graph_sha256(graph: dict[str, Any]) -> str:
    """Hash the complete GET graph, including readonly graph metadata."""
    canonical = json.dumps(
        graph,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


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
        # Keep TaskError intact. If the plugin package is unavailable in an
        # injected/offline caller, fall back to this module's safe error type.
        if getattr(exc, "code", None) == code and getattr(exc, "http_status", None) == status:
            raise
        raise OperationError(
            code,
            stage=stage,
            http_status=status,
        ) from None


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
    """Make one bounded request on the already authenticated canonical client."""
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
                if method in {"PUT", "POST"} and status >= 500:
                    code = "team_config_write_uncertain" if method == "PUT" else "revision_submission_uncertain"
                    raise OperationError(
                        code,
                        stage=stage,
                        http_status=status,
                        submission_uncertain=True,
                        config_write_succeeded=(method == "POST"),
                    )
                _task_error_for_status(status, stage=stage)
            parts: list[bytes] = []
            size = 0
            async for part in response.aiter_bytes():
                size += len(part)
                if size > MAX_RESPONSE_BYTES:
                    if method in {"PUT", "POST"}:
                        code = "team_config_write_uncertain" if method == "PUT" else "revision_submission_uncertain"
                        raise OperationError(
                            code,
                            stage=stage,
                            submission_uncertain=True,
                            config_write_succeeded=(method == "POST"),
                        )
                    raise OperationError("invalid_response", stage=stage)
                parts.append(part)
    except OperationError:
        raise
    except Exception as exc:
        # Preserve the canonical adapter error so aoitalk_auth can invalidate
        # a borrowed session on 401 and apply its shared 429 cooldown.
        if status is not None and getattr(exc, "http_status", None) == status and getattr(exc, "code", None):
            raise
        if method in {"PUT", "POST"}:
            code = "team_config_write_uncertain" if method == "PUT" else "revision_submission_uncertain"
            raise OperationError(
                code,
                stage=stage,
                submission_uncertain=True,
                config_write_succeeded=(method == "POST"),
            ) from None
        raise OperationError("connection_error", stage=stage) from None

    try:
        value = json.loads(b"".join(parts))
    except (ValueError, UnicodeError):
        if method in {"PUT", "POST"}:
            code = "team_config_write_uncertain" if method == "PUT" else "revision_submission_uncertain"
            raise OperationError(
                code,
                stage=stage,
                submission_uncertain=True,
                config_write_succeeded=(method == "POST"),
            ) from None
        raise OperationError("invalid_response", stage=stage) from None
    if not isinstance(value, dict):
        if method in {"PUT", "POST"}:
            code = "team_config_write_uncertain" if method == "PUT" else "revision_submission_uncertain"
            raise OperationError(
                code,
                stage=stage,
                submission_uncertain=True,
                config_write_succeeded=(method == "POST"),
            )
        raise OperationError("invalid_response", stage=stage)
    return value


async def _get_agent_team_config(client: Any, *, stage: str) -> dict[str, Any]:
    envelope = await _request_json(
        client,
        "GET",
        "/api/agent-team/config",
        expected_status=200,
        stage=stage,
    )
    graph = _mapping(envelope.get("agent_team"), stage=stage)
    if graph.get("schema_version") != 3:
        raise OperationError("unsupported_team_schema", stage=stage)
    return graph


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
        or revision.get("agent_team_id") != TEAM_ID
        or revision.get("execution_profile_id") != DEFAULT_EXECUTION_PROFILE_ID
        or revision.get("allowed_subagent_ids") != ALLOWED_SUBAGENT_IDS
    ):
        raise OperationError("base_revision_mismatch", stage="base_revision_get")
    return revision


def _target_team_and_subagent(graph: dict[str, Any], *, stage: str) -> tuple[dict[str, Any], dict[str, Any]]:
    teams = _mapping(graph.get("teams"), stage=stage)
    subagents = _mapping(graph.get("subagents"), stage=stage)
    team = _mapping(teams.get(TEAM_ID), stage=stage, code="target_team_missing")
    subagent = _mapping(subagents.get(SUBAGENT_ID), stage=stage, code="target_subagent_missing")
    members = team.get("subagent_ids")
    if not isinstance(members, list) or SUBAGENT_ID not in members:
        raise OperationError("target_subagent_not_in_team", stage=stage)
    capabilities = subagent.get("capability_ids")
    if (
        not isinstance(capabilities, list)
        or any(not isinstance(item, str) for item in capabilities)
        or not RETAINED_SUBAGENT_CAPABILITIES.issubset(set(capabilities))
    ):
        raise OperationError("target_subagent_capability_mismatch", stage=stage)
    profiles = _mapping(team.get("execution_profiles", {}), stage=stage)
    if DEFAULT_EXECUTION_PROFILE_ID not in profiles:
        raise OperationError("default_execution_profile_missing", stage=stage)
    return team, subagent


def _profile_matches_requested(profile: Any) -> bool:
    if not isinstance(profile, dict):
        return False
    route = profile.get("default_route")
    if not isinstance(route, dict):
        return False
    return (
        profile.get("profile_id") == EXECUTION_PROFILE["profile_id"]
        and profile.get("name") == EXECUTION_PROFILE["name"]
        and profile.get("enabled") is True
        and route.get("inherit_model") is False
        and route.get("provider") == "muse-remote"
        and route.get("backend") == "remote"
        and route.get("model") in (None, "")
        and route.get("effort") in (None, "")
        and route.get("effort_policy") in (None, "not_applicable")
        and profile.get("overrides") == {}
    )


def _build_candidate_graph(graph: dict[str, Any]) -> tuple[dict[str, Any], bool, bool]:
    candidate = copy.deepcopy(graph)
    team, subagent = _target_team_and_subagent(candidate, stage="team_config_get")
    capabilities = subagent["capability_ids"]
    if CAPABILITY_ID not in capabilities:
        subagent["capability_ids"] = [*capabilities, CAPABILITY_ID]
        capability_added = True
    else:
        capability_added = False

    profiles = _mapping(team.get("execution_profiles", {}), stage="team_config_get")
    existing_profile = profiles.get(EXECUTION_PROFILE["profile_id"])
    if existing_profile is None:
        profiles[EXECUTION_PROFILE["profile_id"]] = copy.deepcopy(EXECUTION_PROFILE)
        profile_added = True
    elif _profile_matches_requested(existing_profile):
        profile_added = False
    else:
        raise OperationError("execution_profile_id_conflict", stage="team_config_get")
    team["execution_profiles"] = profiles
    return candidate, capability_added, profile_added


def _revision_post_body(base_revision: dict[str, Any]) -> dict[str, Any]:
    required_fields = (
        "display_name",
        "mission",
        "responsibility_summary",
        "operational_instructions",
        "agent_team_id",
        "execution_profile_id",
        "allowed_subagent_ids",
        "capability_ceiling",
        "wake_policy",
        "budget_policy",
        "concurrency_policy",
    )
    if any(field not in base_revision for field in required_fields):
        raise OperationError("base_revision_mismatch", stage="base_revision_get")
    ceiling = base_revision.get("capability_ceiling")
    if not isinstance(ceiling, list) or any(not isinstance(item, str) for item in ceiling):
        raise OperationError("base_revision_capability_mismatch", stage="base_revision_get")
    if CAPABILITY_ID not in ceiling:
        ceiling = [*ceiling, CAPABILITY_ID]
    return {
        "display_name": base_revision.get("display_name"),
        "mission": base_revision.get("mission"),
        "responsibility_summary": base_revision.get("responsibility_summary"),
        "operational_instructions": base_revision.get("operational_instructions"),
        "agent_team_id": base_revision.get("agent_team_id"),
        "execution_profile_id": base_revision.get("execution_profile_id"),
        "allowed_subagent_ids": copy.deepcopy(base_revision.get("allowed_subagent_ids")),
        "capability_ceiling": ceiling,
        "wake_policy": copy.deepcopy(base_revision.get("wake_policy")),
        "budget_policy": copy.deepcopy(base_revision.get("budget_policy")),
        "concurrency_policy": copy.deepcopy(base_revision.get("concurrency_policy")),
        "idempotency_key": IDEMPOTENCY_KEY,
    }


def _revision_would_gain_capability(
    revision: dict[str, Any],
    *,
    graph_before: dict[str, Any],
    graph_after: dict[str, Any],
) -> bool:
    """Return whether a revision newly reaches the Muse capability.

    This mirrors ``AgentAuthorityResolver._team_capabilities`` for the graph
    data returned by the Agent Team API.  A Team is not a security boundary
    here: every scanned revision is evaluated against its own Team and
    allowed Subagent IDs.  The graph shape is validated before making a
    capability decision so malformed data cannot be treated as a harmless
    no-op during a write operation.
    """

    def _reachable(graph: dict[str, Any]) -> bool:
        teams = _mapping(graph.get("teams"), stage="revision_scan")
        subagents = _mapping(graph.get("subagents"), stage="revision_scan")

        team_id = revision.get("agent_team_id")
        if team_id is None:
            return False
        if not isinstance(team_id, str):
            raise OperationError("invalid_response", stage="revision_scan")
        if team_id not in teams:
            return False
        team = _mapping(teams[team_id], stage="revision_scan")

        team_enabled = team.get("enabled", True)
        if not isinstance(team_enabled, bool):
            raise OperationError("invalid_response", stage="revision_scan")
        if not team_enabled:
            return False

        members = team.get("subagent_ids")
        if members is None:
            return False
        if not isinstance(members, list):
            raise OperationError("invalid_response", stage="revision_scan")
        if any(not isinstance(member, str) for member in members):
            raise OperationError("invalid_response", stage="revision_scan")

        allowed = revision.get("allowed_subagent_ids")
        if allowed is None:
            return False
        if not isinstance(allowed, list):
            raise OperationError("invalid_response", stage="revision_scan")
        if any(not isinstance(item, str) for item in allowed):
            raise OperationError("invalid_response", stage="revision_scan")
        allowed_ids = {str(item).strip() for item in allowed}

        ceiling = revision.get("capability_ceiling")
        if ceiling is None:
            return False
        if not isinstance(ceiling, list):
            raise OperationError("invalid_response", stage="revision_scan")
        if any(not isinstance(item, str) for item in ceiling):
            raise OperationError("invalid_response", stage="revision_scan")
        ceiling_ids = {str(item).strip() for item in ceiling}
        if CAPABILITY_ID not in ceiling_ids:
            return False

        capability_union: set[str] = set()
        for member in members:
            # Match the canonical resolver: Team member IDs are compared
            # without trimming, while revision allow-list values are trimmed.
            member_id = member
            if member_id not in allowed_ids:
                continue
            if member_id not in subagents:
                continue
            subagent = _mapping(subagents[member_id], stage="revision_scan")
            subagent_enabled = subagent.get("enabled", True)
            if not isinstance(subagent_enabled, bool):
                raise OperationError("invalid_response", stage="revision_scan")
            if not subagent_enabled:
                continue
            if "capability_ids" not in subagent:
                continue
            capability_ids = subagent["capability_ids"]
            if capability_ids is None:
                raise OperationError("invalid_response", stage="revision_scan")
            if not isinstance(capability_ids, list):
                raise OperationError("invalid_response", stage="revision_scan")
            if any(not isinstance(capability, str) for capability in capability_ids):
                raise OperationError("invalid_response", stage="revision_scan")
            capability_union.update(capability_ids)
        return CAPABILITY_ID in capability_union

    return not _reachable(graph_before) and _reachable(graph_after)


async def _scan_agent_revisions(
    client: Any,
    *,
    graph_before: dict[str, Any],
    graph_after: dict[str, Any],
) -> list[dict[str, Any]]:
    agents_envelope = await _request_json(
        client,
        "GET",
        "/api/agents",
        expected_status=200,
        stage="agent_revision_scan",
        params={"limit": MAX_COLLECTION_ITEMS},
    )
    agents = agents_envelope.get("agents")
    if not isinstance(agents, list) or not isinstance(agents_envelope.get("total"), int):
        raise OperationError("invalid_agent_list", stage="agent_revision_scan")
    if len(agents) != agents_envelope["total"] or len(agents) >= MAX_COLLECTION_ITEMS:
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
        revisions_envelope = await _request_json(
            client,
            "GET",
            f"/api/agents/{agent_id}/revisions",
            expected_status=200,
            stage="agent_revision_scan",
            params={"limit": MAX_COLLECTION_ITEMS},
        )
        revisions = revisions_envelope.get("revisions")
        if not isinstance(revisions, list) or not isinstance(revisions_envelope.get("total"), int):
            raise OperationError("invalid_revision_list", stage="agent_revision_scan")
        if len(revisions) != revisions_envelope["total"] or len(revisions) >= MAX_COLLECTION_ITEMS:
            raise OperationError("agent_revision_scan_incomplete", stage="agent_revision_scan")
        for revision in revisions:
            if not isinstance(revision, dict):
                raise OperationError("invalid_revision_list", stage="agent_revision_scan")
            if _revision_would_gain_capability(
                revision,
                graph_before=graph_before,
                graph_after=graph_after,
            ):
                conflicts.append(
                    {
                        "agent_id": agent_id,
                        "agent_revision_id": str(revision.get("id") or ""),
                        "version": revision.get("version"),
                    }
                )
    if not target_seen:
        raise OperationError("target_agent_missing", stage="agent_revision_scan")
    return conflicts


def _writable_graph(graph: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(graph)
    for key in ("main_effective_route", "capability_catalog", "effective_route", "provider_model", "llm_profiles"):
        result.pop(key, None)
    subagents = result.get("subagents")
    if isinstance(subagents, dict):
        for subagent in subagents.values():
            if isinstance(subagent, dict):
                for key in ("team_ids", "effective_route", "llm_profile_id"):
                    subagent.pop(key, None)
    return result


def _expected_normalized_graph(candidate: dict[str, Any]) -> dict[str, Any]:
    expected = _writable_graph(candidate)
    profile = expected["teams"][TEAM_ID]["execution_profiles"][EXECUTION_PROFILE["profile_id"]]
    profile["default_route"] = {
        "inherit_model": False,
        "provider": "muse-remote",
        "model": None,
        "effort_policy": "not_applicable",
        "effort": None,
        "backend": "remote",
    }
    return expected


def _assert_config_response_preserved(
    response: dict[str, Any],
    *,
    candidate: dict[str, Any],
) -> None:
    returned = response.get("agent_team")
    returned = _mapping(returned, stage="team_config_put_response")
    if returned.get("schema_version") != 3:
        raise OperationError(
            "team_config_response_did_not_preserve_remote_route",
            stage="team_config_put_response",
            config_write_succeeded=True,
        )
    if _writable_graph(returned) != _expected_normalized_graph(candidate):
        raise OperationError(
            "team_config_response_did_not_preserve_remote_route",
            stage="team_config_put_response",
            config_write_succeeded=True,
        )
    _, subagent = _target_team_and_subagent(returned, stage="team_config_put_response")
    team = returned["teams"][TEAM_ID]
    profile = team["execution_profiles"][EXECUTION_PROFILE["profile_id"]]
    if (
        CAPABILITY_ID not in subagent["capability_ids"]
        or profile.get("default_route", {}).get("backend") != "remote"
        or profile.get("default_route", {}).get("provider") != "muse-remote"
    ):
        raise OperationError(
            "team_config_response_did_not_preserve_remote_route",
            stage="team_config_put_response",
            config_write_succeeded=True,
        )


def _verify_revision_response(
    response: dict[str, Any],
    *,
    body: dict[str, Any],
) -> dict[str, Any]:
    revision = _mapping(response.get("revision"), stage="revision_post_response")
    if (
        _uuid(revision.get("agent_id")) != AGENT_ID
        or revision.get("agent_team_id") != TEAM_ID
        or revision.get("execution_profile_id") != DEFAULT_EXECUTION_PROFILE_ID
        or CAPABILITY_ID not in (revision.get("capability_ceiling") or [])
        or any(
            revision.get(field) != body.get(field)
            for field in (
                "display_name",
                "mission",
                "responsibility_summary",
                "operational_instructions",
                "agent_team_id",
                "execution_profile_id",
                "allowed_subagent_ids",
                "capability_ceiling",
                "wake_policy",
                "budget_policy",
                "concurrency_policy",
            )
        )
    ):
        raise OperationError(
            "revision_response_did_not_match_request",
            stage="revision_post",
            submission_uncertain=True,
            config_write_succeeded=True,
        )
    return {
        "agent_revision_id": revision.get("id"),
        "version": revision.get("version"),
    }


async def _apply_with_client(
    client: Any,
    *,
    expected_graph_sha256: str,
) -> dict[str, Any]:
    graph = await _get_agent_team_config(client, stage="team_config_get")
    before_hash = graph_sha256(graph)
    if before_hash != expected_graph_sha256:
        raise OperationError("graph_hash_conflict", stage="initial_graph_check")
    base_revision = await _get_base_revision(client)
    candidate, capability_added, profile_added = _build_candidate_graph(graph)
    post_body = _revision_post_body(base_revision)
    conflicts = await _scan_agent_revisions(
        client,
        graph_before=graph,
        graph_after=candidate,
    )
    if conflicts:
        raise OperationError(
            "other_revision_authority_conflict",
            stage="agent_revision_scan",
            blocking_revisions=conflicts,
        )

    config_changed = candidate != graph
    config_write_succeeded = False
    if config_changed:
        fresh_graph = await _get_agent_team_config(client, stage="fresh_graph_check")
        if graph_sha256(fresh_graph) != before_hash:
            raise OperationError("graph_hash_conflict", stage="fresh_graph_check")
        put_response = await _request_json(
            client,
            "PUT",
            "/api/agent-team/config",
            expected_status=200,
            stage="team_config_put",
            payload={"agent_team": candidate},
        )
        config_write_succeeded = True
        _assert_config_response_preserved(put_response, candidate=candidate)
    else:
        # Recheck even when the graph is already in the requested state before
        # creating the revision. This catches a concurrent graph replacement.
        fresh_graph = await _get_agent_team_config(client, stage="fresh_graph_check")
        if graph_sha256(fresh_graph) != before_hash:
            raise OperationError("graph_hash_conflict", stage="fresh_graph_check")

    try:
        post_response = await _request_json(
            client,
            "POST",
            f"/api/agents/{AGENT_ID}/revisions",
            expected_status=201,
            stage="revision_post",
            payload=post_body,
            headers={"Idempotency-Key": IDEMPOTENCY_KEY},
        )
    except OperationError as exc:
        exc.config_write_succeeded = config_write_succeeded
        raise
    try:
        posted = _verify_revision_response(post_response, body=post_body)
    except OperationError as exc:
        exc.stage = "revision_post"
        exc.submission_uncertain = True
        exc.config_write_succeeded = config_write_succeeded
        raise
    return {
        "ok": True,
        "mode": "apply",
        "agent_id": AGENT_ID,
        "agent_slug": AGENT_SLUG,
        "base_revision_id": BASE_REVISION_ID,
        "team_id": TEAM_ID,
        "execution_profile_id": DEFAULT_EXECUTION_PROFILE_ID,
        "team_capability_added": capability_added,
        "execution_profile_added": profile_added,
        "config_write_attempted": config_changed,
        "config_write_succeeded": config_write_succeeded,
        "revision_post_attempted": True,
        "idempotency_key": IDEMPOTENCY_KEY,
        "graph_sha256": before_hash,
        **posted,
    }


async def _prepare_with_client(client: Any) -> dict[str, Any]:
    graph = await _get_agent_team_config(client, stage="team_config_get")
    before_hash = graph_sha256(graph)
    base_revision = await _get_base_revision(client)
    candidate, capability_added, profile_added = _build_candidate_graph(graph)
    post_body = _revision_post_body(base_revision)
    conflicts = await _scan_agent_revisions(
        client,
        graph_before=graph,
        graph_after=candidate,
    )
    if conflicts:
        raise OperationError(
            "other_revision_authority_conflict",
            stage="agent_revision_scan",
            blocking_revisions=conflicts,
        )
    return {
        "ok": True,
        "mode": "prepare",
        "agent_id": AGENT_ID,
        "agent_slug": AGENT_SLUG,
        "base_revision_id": BASE_REVISION_ID,
        "base_revision_version": base_revision.get("version"),
        "team_id": TEAM_ID,
        "subagent_id": SUBAGENT_ID,
        "execution_profile_id": DEFAULT_EXECUTION_PROFILE_ID,
        "capability_id": CAPABILITY_ID,
        "team_capability_added": capability_added,
        "team_capability_ids_before": copy.deepcopy(
            graph["subagents"][SUBAGENT_ID]["capability_ids"]
        ),
        "team_capability_ids_after": copy.deepcopy(
            candidate["subagents"][SUBAGENT_ID]["capability_ids"]
        ),
        "execution_profile_added": profile_added,
        "revision_capability_added": CAPABILITY_ID not in base_revision.get("capability_ceiling", []),
        "revision_capability_ceiling_before": copy.deepcopy(
            base_revision["capability_ceiling"]
        ),
        "revision_capability_ceiling_after": copy.deepcopy(post_body["capability_ceiling"]),
        "idempotency_key": IDEMPOTENCY_KEY,
        "graph_sha256": before_hash,
        "proposed_graph_sha256": graph_sha256(candidate),
        "execution_profile": copy.deepcopy(EXECUTION_PROFILE),
        "revision_fields_preserved": [
            "display_name",
            "mission",
            "responsibility_summary",
            "operational_instructions",
            "agent_team_id",
            "execution_profile_id",
            "allowed_subagent_ids",
            "wake_policy",
            "budget_policy",
            "concurrency_policy",
        ],
        "other_revisions_would_gain": [],
        "config_put_would_be_needed": candidate != graph,
        "revision_post_would_be_needed": True,
        "write_attempted": False,
        "apply_invocation": (
            "python scripts/prepare_crowdworks_muse_remote_revision.py --apply "
            f"--expected-graph-sha256 {before_hash}"
        ),
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
    expected_graph_sha256: str | None = None,
    session_runner: Callable[[Callable[[Any], Awaitable[dict[str, Any]]]], Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """Run one prepare or apply pass using an injectable authenticated session."""
    if apply:
        if not isinstance(expected_graph_sha256, str) or not GRAPH_HASH_RE.fullmatch(
            expected_graph_sha256
        ):
            raise OperationError("expected_graph_sha256_required", stage="arguments")
    elif expected_graph_sha256 is not None:
        raise OperationError("unexpected_graph_sha256", stage="arguments")

    runner = session_runner or _canonical_session
    result: dict[str, Any] = {}

    async def callback(client: Any) -> dict[str, Any]:
        nonlocal result
        if apply:
            assert expected_graph_sha256 is not None
            result = await _apply_with_client(
                client,
                expected_graph_sha256=expected_graph_sha256,
            )
        else:
            result = await _prepare_with_client(client)
        # The plugin's sanitizer sees only this bounded status; it never needs
        # the full Agent Team graph or revision content.
        return {"ok": True, "status": "completed"}

    await runner(callback)
    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the Agent Team PUT and idempotent Agent revision POST",
    )
    parser.add_argument(
        "--expected-graph-sha256",
        help="graph SHA-256 emitted by a prior read-only prepare invocation",
    )
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    try:
        result = await run_helper(
            apply=args.apply,
            expected_graph_sha256=args.expected_graph_sha256,
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
