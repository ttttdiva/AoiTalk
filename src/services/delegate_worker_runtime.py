"""AgentDialogue bridge for existing managed CLI and Muse sessions.

This module only admits and observes sessions through
``CLIDevelopmentSessionService``.  It never constructs a CLI process or calls
the provider directly.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID


_KIND_PROVIDER = {
    "codex": "codex-cli",
    "antigravity": "antigravity-cli",
    "claude": "claude-cli",
    "grok": "grok-cli",
    "muse": "muse-remote",
}
_MAX_SCOPE_PATHS = 64
_MAX_PATH_CHARS = 512
_MAX_TIMEOUT_SECONDS = 3600
_MAX_RESULT_TEXT = 30_000


class DelegateWorkerRuntimeError(RuntimeError):
    """Bounded, safe error returned by the worker runtime bridge."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "delegate_worker_runtime_failed")
        super().__init__(self.code)


def _sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _clean_uuid(value: Any, *, code: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise DelegateWorkerRuntimeError(code) from None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _canonical_scope(
    workspace_scope: Any,
    *,
    revalidate_current_scope: bool,
) -> dict[str, Any]:
    """Return absolute canonical directory roots within both trusted scopes."""

    from ..security.agent_run_scope import AgentRunScope, get_current_run_scope

    if not isinstance(workspace_scope, Mapping):
        raise DelegateWorkerRuntimeError("worker_scope_invalid")
    mode = str(workspace_scope.get("mode") or "").strip().casefold()
    raw_root = workspace_scope.get("root")
    raw_paths = workspace_scope.get("paths")
    if mode not in {"read", "write"} or not isinstance(raw_root, str) or not isinstance(raw_paths, (list, tuple)):
        raise DelegateWorkerRuntimeError("worker_scope_invalid")
    if not raw_paths or len(raw_paths) > _MAX_SCOPE_PATHS:
        raise DelegateWorkerRuntimeError("worker_scope_invalid")

    root = Path(raw_root).expanduser().absolute().resolve()
    if not root.exists() or not root.is_dir():
        raise DelegateWorkerRuntimeError("worker_scope_root_unavailable")
    current_scope = get_current_run_scope() if revalidate_current_scope else None
    if revalidate_current_scope:
        if not isinstance(current_scope, AgentRunScope):
            raise DelegateWorkerRuntimeError("worker_run_scope_missing")
        current_root = Path(current_scope.canonical_root).resolve()
        if root != current_root:
            raise DelegateWorkerRuntimeError("worker_scope_root_mismatch")
        if mode == "write" and current_scope.workspace_access_level != "write":
            raise DelegateWorkerRuntimeError("worker_scope_write_denied")

    canonical_paths: list[str] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise DelegateWorkerRuntimeError("worker_scope_path_invalid")
        supplied = Path(raw_path).expanduser()
        if not supplied.is_absolute():
            supplied = root.joinpath(supplied)
        path = supplied.absolute().resolve()
        if not _is_within(path, root) or not path.exists() or not path.is_dir():
            raise DelegateWorkerRuntimeError("worker_scope_path_outside_root")
        if revalidate_current_scope:
            try:
                if mode == "write":
                    current_scope.assert_mutation_allowed(path, "write")
                else:
                    current_scope.assert_read_allowed(path)
            except Exception:
                raise DelegateWorkerRuntimeError("worker_scope_path_outside_agent_scope") from None
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            canonical_paths.append(str(path))
    return {"mode": mode, "root": str(root), "paths": canonical_paths}


def _scope_marker(
    *,
    dialogue_id: str,
    kind: str,
    workspace_scope: Mapping[str, Any],
    timeout_seconds: int | None,
    budgets: Mapping[str, Any] | None,
) -> dict[str, Any]:
    scope = {
        "mode": str(workspace_scope["mode"]),
        "root": str(workspace_scope["root"]),
        "paths": [str(item) for item in workspace_scope["paths"]],
    }
    normalized_budgets = {
        str(key): int(value)
        for key, value in (budgets or {}).items()
        if key in {"max_tokens", "max_cost_micros", "max_attempts"}
        and isinstance(value, int)
        and not isinstance(value, bool)
    }
    return {
        "schema_version": 1,
        "dialogue_id": dialogue_id,
        "kind": kind,
        "workspace_scope": scope,
        "workspace_scope_sha256": _sha256(scope),
        "timeout_seconds": timeout_seconds,
        "budgets": normalized_budgets,
    }


def _resolve_allow_cli_native_tools(
    config: Any, revision: Any, *, kind: str, mode: str
) -> bool:
    """Require a complete pinned revision and scoped execution capabilities."""

    del config  # Route resolution is separate; this is a revision-only gate.
    normalized_kind = str(kind or "").strip().casefold()
    normalized_mode = str(mode or "").strip().casefold()
    if normalized_kind == "muse" or normalized_mode not in {"read", "write"}:
        return False
    try:
        from .agent_delegate_policy import (
            revision_has_new_delegate_policy,
            worker_delegate_required_capabilities,
        )

        # Incomplete historical revisions cannot prove the execution ceiling.
        if not revision_has_new_delegate_policy(revision):
            return False
        raw_capabilities = (
            revision.get("capability_ceiling_json", revision.get("capability_ceiling"))
            if isinstance(revision, Mapping)
            else getattr(revision, "capability_ceiling_json", None)
        )
        if isinstance(raw_capabilities, Mapping):
            revision_capabilities = {
                str(key).strip().casefold()
                for key, enabled in raw_capabilities.items()
                if enabled is True and str(key).strip()
            }
        elif isinstance(raw_capabilities, (list, tuple, set, frozenset)):
            revision_capabilities = {
                str(item).strip().casefold()
                for item in raw_capabilities
                if isinstance(item, str) and item.strip()
            }
        else:
            return False
        required = worker_delegate_required_capabilities(
            normalized_kind, normalized_mode
        )
        return bool(required) and required.issubset(revision_capabilities)
    except Exception:
        return False


def _read_marker(session_context: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = session_context.get("delegate_worker")
    if not isinstance(raw, Mapping):
        return None
    return dict(raw)


def _worker_local_session_is_terminal(projection: Any) -> bool:
    """Require both the provider Run and its Work lease to be drained."""

    if not isinstance(projection, Mapping):
        return False
    run = projection.get("run") if isinstance(projection.get("run"), Mapping) else {}
    work = projection.get("work") if isinstance(projection.get("work"), Mapping) else {}
    run_status = str(run.get("status") or "").casefold()
    work_status = str(work.get("state") or work.get("status") or "").casefold()
    return run_status in {"succeeded", "failed", "cancelled"} and work_status in {
        "succeeded",
        "failed",
        "cancelled",
        "dead_letter",
        "uncertain",
        "permanent",
    }


def _safe_worker_result_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        from .outbound_privacy_service import redact_secret_for_local_display

        redacted = str(redact_secret_for_local_display(value))
    except Exception:
        return None
    return redacted[:limit]


def _safe_worker_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    allowed = {
        "input_tokens",
        "prompt_tokens",
        "output_tokens",
        "completion_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cost_micros",
        "estimated_cost_micros",
    }
    usage: dict[str, int] = {}
    for key, raw in list(value.items())[:32]:
        normalized = str(key).strip().casefold()
        if (
            normalized in allowed
            and isinstance(raw, int)
            and not isinstance(raw, bool)
            and 0 <= raw <= 10**15
        ):
            usage[normalized] = raw
    return usage or None


def _scoped_changed_files(value: Any, workspace_scope: Any) -> list[str]:
    if not isinstance(value, list) or not isinstance(workspace_scope, Mapping):
        return []
    raw_root = workspace_scope.get("root")
    raw_paths = workspace_scope.get("paths")
    if not isinstance(raw_root, str) or not isinstance(raw_paths, list):
        return []
    try:
        root = Path(raw_root).resolve(strict=True)
        allowed_paths = [Path(str(item)).resolve(strict=True) for item in raw_paths]
    except (OSError, RuntimeError, TypeError, ValueError):
        return []
    if not root.is_dir() or not allowed_paths or any(not path.is_dir() for path in allowed_paths):
        return []
    output: list[str] = []
    for item in value[:128]:
        if not isinstance(item, str) or not item.strip():
            continue
        try:
            supplied = Path(item)
            candidate = (supplied if supplied.is_absolute() else root / supplied).resolve(
                strict=False
            )
            if not any(candidate == base or _is_within(candidate, base) for base in allowed_paths):
                continue
            relative = candidate.relative_to(root)
        except (OSError, RuntimeError, TypeError, ValueError, IndexError):
            continue
        output.append(str(relative)[:_MAX_PATH_CHARS])
    return output


def _extract_worker_result(
    session: Mapping[str, Any], workspace_scope: Any = None
) -> dict[str, Any] | None:
    run = session.get("run") if isinstance(session.get("run"), Mapping) else {}
    work = session.get("work") if isinstance(session.get("work"), Mapping) else {}
    run_result = run.get("result") if isinstance(run.get("result"), Mapping) else {}
    work_result = work.get("result_summary_json") if isinstance(work.get("result_summary_json"), Mapping) else {}
    run_worker_result = (
        run_result.get("delegate_worker_result")
        if isinstance(run_result.get("delegate_worker_result"), Mapping)
        else {}
    )
    work_worker_result = (
        work_result.get("delegate_worker_result")
        if isinstance(work_result.get("delegate_worker_result"), Mapping)
        else {}
    )

    assistant_text = (
        run_result.get("assistant_text")
        or work_result.get("assistant_text")
        or run_worker_result.get("summary")
        or work_worker_result.get("summary")
    )
    if not isinstance(assistant_text, str):
        assistant_text = run_result.get("summary") or work_result.get("summary")
    assistant_text = _safe_worker_result_text(
        assistant_text, limit=_MAX_RESULT_TEXT
    )
    summary = assistant_text.strip()[:3000] if assistant_text is not None else None

    raw_diff = next(
        (
            value[:_MAX_RESULT_TEXT]
            for value in (
                run_result.get("diff"),
                run_result.get("patch"),
                work_result.get("diff"),
                work_result.get("patch"),
                run_worker_result.get("diff"),
                work_worker_result.get("diff"),
            )
            if isinstance(value, str) and value
        ),
        None,
    )
    diff = _safe_worker_result_text(raw_diff, limit=_MAX_RESULT_TEXT)
    changed_file_values = next(
        (value for value in (
            run_result.get("changed_files"),
            work_result.get("changed_files"),
            run_worker_result.get("changed_files"),
            work_worker_result.get("changed_files"),
        ) if isinstance(value, list)),
        [],
    )
    changed_files = _scoped_changed_files(changed_file_values, workspace_scope)
    artifact_refs = next(
        (
            [
                text
                for item in value[:64]
                if (text := _safe_worker_result_text(item, limit=_MAX_PATH_CHARS))
                is not None
            ]
            for value in (
                run_result.get("artifact_refs"),
                run_result.get("artifacts"),
                work_result.get("artifact_refs"),
                run_worker_result.get("artifact_refs"),
                work_worker_result.get("artifact_refs"),
            )
            if isinstance(value, list)
        ),
        [],
    )
    usage = (
        run_result.get("usage")
        or work_result.get("usage")
        or run_worker_result.get("usage")
        or work_worker_result.get("usage")
        or run.get("usage")
    )
    evidence_source = run_worker_result if run_worker_result else work_worker_result
    change_evidence_status = evidence_source.get("change_evidence_status")
    if not isinstance(change_evidence_status, str) or not re.fullmatch(
        r"[a-z0-9_.-]{1,80}", change_evidence_status.casefold()
    ):
        change_evidence_status = None
    change_evidence_available = evidence_source.get("change_evidence_available")
    if not isinstance(change_evidence_available, bool):
        change_evidence_available = None
    return {
        "summary": summary,
        "assistant_text": assistant_text,
        "diff": diff,
        "changed_files": changed_files,
        "artifact_refs": artifact_refs,
        "usage": _safe_worker_usage(usage),
        "change_evidence_available": change_evidence_available,
        "change_evidence_status": change_evidence_status,
    }


@dataclass(frozen=True, slots=True)
class DelegateWorkerSessionContext:
    """Concrete internal marker; model/API mappings are never accepted as it."""

    value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.value)


@dataclass(frozen=True, slots=True)
class DelegateWorkerRunLimits:
    """Internal limit marker for one already-authorized Dialogue run."""

    value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.value)


class DelegateWorkerRuntime:
    """Prepare, start and control managed sessions for one AgentDialogue."""

    def __init__(
        self,
        *,
        cli_session_service: Any | None = None,
        db_manager: Any | None = None,
        config: Any | None = None,
    ) -> None:
        self.cli_session_service = cli_session_service
        self.db_manager = db_manager
        self.config = config

    def _service(self) -> Any:
        if self.cli_session_service is not None:
            return self.cli_session_service
        from .cli_development_sessions import get_shared_cli_development_session_service

        service = get_shared_cli_development_session_service(self.db_manager, config=self.config)
        if service is None:
            raise DelegateWorkerRuntimeError("worker_session_service_unavailable")
        self.cli_session_service = service
        return service

    async def prepare_session(
        self,
        owner_id: str,
        actor_role: str | None,
        agent_id: str,
        agent_revision_id: str,
        kind: str,
        model: str | None,
        effort: str | None,
        workspace_scope: Mapping[str, Any],
        timeout_seconds: int | None,
        dialogue_idempotency_key: str,
        project_id: str | None,
        *,
        dialogue_id: str | None = None,
        budgets: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_kind = str(kind or "").strip().casefold()
        provider = _KIND_PROVIDER.get(normalized_kind)
        if provider is None:
            raise DelegateWorkerRuntimeError("worker_kind_invalid")
        owner = _clean_uuid(owner_id, code="worker_owner_invalid")
        agent = _clean_uuid(agent_id, code="worker_agent_invalid")
        revision = _clean_uuid(agent_revision_id, code="worker_revision_invalid")
        dialogue_id = _clean_uuid(
            dialogue_id if dialogue_id is not None else dialogue_idempotency_key,
            code="worker_dialogue_id_invalid",
        )
        session_idempotency_key = str(dialogue_idempotency_key or "").strip()
        if not session_idempotency_key:
            raise DelegateWorkerRuntimeError("worker_session_idempotency_key_invalid")
        scope = _canonical_scope(workspace_scope, revalidate_current_scope=True)
        timeout = _bounded_timeout(timeout_seconds)
        normalized_budgets = _normalize_runtime_budgets(budgets)
        marker_value = _scope_marker(
            dialogue_id=dialogue_id,
            kind=normalized_kind,
            workspace_scope=scope,
            timeout_seconds=timeout,
            budgets=normalized_budgets,
        )
        marker_value["owner_id"] = owner
        marker_value["agent_id"] = agent
        marker_value["agent_revision_id"] = revision
        # External workers never inherit AoiTalk AgentDialogue tools.  This is
        # copied into the managed session marker and checked again by the
        # execution adapter before any provider/tool call.
        marker_value["agent_dialogue_tool_ids"] = []
        service = self._service()
        db = await service._session()
        try:
            from ..memory.models import AgentRevision
            from .agent_delegate_policy import (
                revision_has_new_delegate_policy,
                worker_delegate_required_capabilities,
            )
            from .agent_model_routing import AgentRoutingPins, resolve_agent_role_route

            revision_row = await db.get(AgentRevision, UUID(revision))
            if revision_row is None or str(revision_row.agent_id) != agent:
                raise DelegateWorkerRuntimeError("worker_revision_invalid")
            route = resolve_agent_role_route(
                self.config if self.config is not None else getattr(service, "config", None),
                AgentRoutingPins.from_revision(revision_row),
                "operator",
            )
            if str(route.provider or "").strip().casefold() != provider:
                raise DelegateWorkerRuntimeError("worker_route_kind_mismatch")
            if normalized_kind != "muse":
                pinned_model = str(route.model or "").strip()
                pinned_effort = str(route.effort or "").strip()
                if not pinned_model:
                    raise DelegateWorkerRuntimeError("worker_route_model_unavailable")
                if model not in (None, "", pinned_model):
                    raise DelegateWorkerRuntimeError("worker_model_override_not_allowed")
                if effort not in (None, "", pinned_effort):
                    raise DelegateWorkerRuntimeError("worker_effort_override_not_allowed")
                resolved_model = pinned_model
                resolved_effort = pinned_effort
            else:
                resolved_model = None
                resolved_effort = ""
            if not revision_has_new_delegate_policy(revision_row):
                raise DelegateWorkerRuntimeError("worker_revision_policy_denied")
            allow_native = _resolve_allow_cli_native_tools(
                self.config if self.config is not None else getattr(service, "config", None),
                revision_row,
                kind=normalized_kind,
                mode=str(scope["mode"]),
            )
            raw_revision_caps = getattr(revision_row, "capability_ceiling_json", None)
            if isinstance(raw_revision_caps, Mapping):
                revision_caps = {
                    str(key).strip().casefold()
                    for key, enabled in raw_revision_caps.items()
                    if enabled is True and str(key).strip()
                }
            else:
                revision_caps = {
                    str(item).strip().casefold()
                    for item in (raw_revision_caps or ())
                    if isinstance(item, str) and item.strip()
                }
            requested_caps = (
                {"remote_agent_delegate"}
                if normalized_kind == "muse"
                else set(worker_delegate_required_capabilities(normalized_kind, scope["mode"]))
            )
            if not requested_caps <= revision_caps:
                raise DelegateWorkerRuntimeError("worker_revision_scope_capability_missing")
            if normalized_kind not in {"claude", "muse"}:
                # The managed CLI adapter currently has provider-enforced
                # capability ceilings only for Claude.  Do not admit a
                # narrower workspace scope through an unenforceable backend.
                raise DelegateWorkerRuntimeError(
                    "worker_capability_ceiling_unenforceable"
                )
            worker_capabilities = sorted(requested_caps & revision_caps)
        finally:
            await service._close(db)
        if normalized_kind != "muse" and not allow_native:
            raise DelegateWorkerRuntimeError("worker_native_tools_not_allowed")
        marker_value["allow_cli_native_tools"] = allow_native
        marker_value["capabilities"] = worker_capabilities
        marker = DelegateWorkerSessionContext(marker_value)
        request: dict[str, Any] = {
            "owner_id": owner,
            "actor_role": actor_role,
            "agent_id": agent,
            "agent_revision_id": revision,
            "provider": provider,
            "idempotency_key": (
                session_idempotency_key
                if session_idempotency_key != dialogue_id
                else f"delegate-worker:{dialogue_id}"
            ),
            "title": f"External worker {normalized_kind}",
            "project_id": project_id,
            "role_context_mode": "raw",
        }
        if normalized_kind == "muse":
            request["execution_target"] = "remote_muse"
        else:
            request.update(
                {
                    "model": resolved_model,
                    "effort": resolved_effort,
                    "working_directory": scope["paths"][0],
                    "allowed_capabilities": tuple(worker_capabilities),
                }
            )
        try:
            result = await service.create_session(
                request,
                _delegate_worker_context=marker,
            )
        except Exception as exc:
            _raise_runtime_error(exc, fallback="worker_session_prepare_failed")
        session_id = str(result.get("session_id") or "") if isinstance(result, Mapping) else ""
        if not session_id:
            raise DelegateWorkerRuntimeError("worker_session_prepare_failed")
        return {
            "success": True,
            "session_id": session_id,
            "agent_id": agent,
            "agent_revision_id": revision,
            "worker_kind": normalized_kind,
            "status": str(result.get("status") or "ready"),
            "workspace_scope_sha256": marker_value["workspace_scope_sha256"],
            "allow_cli_native_tools": allow_native,
            # The provider may only report usage after it has returned. Keep
            # the exact bounded request visible to the caller so the durable
            # Dialogue reservation can be settled post hoc, including an
            # uninterruptible overrun.
            "budgets": dict(normalized_budgets),
            "timeout_seconds": timeout,
        }

    async def discard_prepared_session(
        self,
        session_id: str,
        *,
        owner_id: str,
        dialogue_id: str,
    ) -> dict[str, Any]:
        """Tombstone a prepared session only while it has no Run or Work.

        This closes the prepare/Dialogue-commit gap without deleting related
        audit rows or risking a session whose send has already started.
        """

        sid = _clean_uuid(session_id, code="worker_session_id_invalid")
        did = _clean_uuid(dialogue_id, code="worker_dialogue_id_invalid")
        owner = _clean_uuid(owner_id, code="worker_owner_invalid")
        service = self._service()
        db = await service._session()
        try:
            from sqlalchemy import select

            from ..memory.models import AgentRun, AgentWorkItem, ConversationSession
            from .cli_development_sessions import CLI_DEVELOPMENT_CONTEXT_KEY

            statement = (
                select(ConversationSession)
                .where(ConversationSession.id == UUID(sid))
                .with_for_update()
            )
            session = (await db.execute(statement)).scalars().first()
            if session is None or str(session.user_id) != owner:
                raise DelegateWorkerRuntimeError("worker_session_not_found")
            context = session.context if isinstance(session.context, Mapping) else {}
            cli_context = context.get(CLI_DEVELOPMENT_CONTEXT_KEY)
            cli_context = cli_context if isinstance(cli_context, Mapping) else {}
            marker = cli_context.get("delegate_worker")
            if not isinstance(marker, Mapping) or str(marker.get("dialogue_id") or "") != did:
                raise DelegateWorkerRuntimeError("worker_session_dialogue_mismatch")
            has_run = (
                await db.execute(
                    select(AgentRun.id)
                    .where(AgentRun.session_id == UUID(sid))
                    .limit(1)
                )
            ).first()
            has_work = (
                await db.execute(
                    select(AgentWorkItem.id)
                    .where(AgentWorkItem.conversation_session_id == UUID(sid))
                    .limit(1)
                )
            ).first()
            if has_run is not None or has_work is not None:
                raise DelegateWorkerRuntimeError("worker_session_already_started")
            if getattr(session, "deleted_at", None) is None:
                session.deleted_at = service._clock()
                session.development_status = "failed"
                next_context = dict(context)
                next_cli_context = dict(cli_context)
                next_cli_context["status"] = "discarded_before_start"
                next_context[CLI_DEVELOPMENT_CONTEXT_KEY] = next_cli_context
                session.context = next_context
            await db.commit()
        except DelegateWorkerRuntimeError:
            raise
        except Exception as exc:
            try:
                await db.rollback()
            except Exception:
                pass
            _raise_runtime_error(exc, fallback="worker_prepared_session_discard_failed")
        finally:
            await service._close(db)
        return {"success": True, "session_id": sid, "dialogue_id": did, "status": "discarded"}

    async def prepare_resume_session(
        self,
        *,
        previous_session_id: str,
        owner_id: str,
        dialogue_id: str,
        project_id: str | None,
        workspace_scope: Mapping[str, Any],
        timeout_seconds: int | None,
        control_revision: int,
        budgets: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Prepare a fresh managed session for a terminal safe continuation."""

        sid = _clean_uuid(previous_session_id, code="worker_session_id_invalid")
        did = _clean_uuid(dialogue_id, code="worker_dialogue_id_invalid")
        old = await self._validate_dialogue_session(
            sid,
            did,
            owner_id=owner_id,
            workspace_scope=workspace_scope,
        )
        if old["kind"] == "muse":
            service = self._service()
            try:
                local_projection = await service.get_session(sid, owner_id=str(owner_id))
            except Exception as exc:
                _raise_runtime_error(exc, fallback="worker_resume_state_unavailable")
            if not _worker_local_session_is_terminal(local_projection):
                try:
                    recovered = await service.reconnect(sid, owner_id=str(owner_id))
                except Exception as exc:
                    _raise_runtime_error(exc, fallback="worker_resume_state_unavailable")
                safe_reconnect = recovered.get("remote_reconnect") if isinstance(recovered, Mapping) else None
                if (
                    not isinstance(safe_reconnect, Mapping)
                    or safe_reconnect.get("safe_to_send") is not True
                    or str(recovered.get("status") or "") not in {"completed", "failed"}
                ):
                    raise DelegateWorkerRuntimeError("muse_remote_resume_state_unconfirmed")

        service = self._service()
        db = await service._session()
        try:
            from ..memory.models import AgentRevision

            revision = await db.get(AgentRevision, UUID(str(old["agent_revision_id"])))
            if revision is None:
                raise DelegateWorkerRuntimeError("worker_revision_invalid")
            config = self.config if self.config is not None else getattr(service, "config", None)
            from .agent_model_routing import AgentRoutingPins, resolve_agent_role_route

            route = resolve_agent_role_route(
                config,
                AgentRoutingPins.from_revision(revision),
                "operator",
            )
            model = str(route.model or "") if old["kind"] != "muse" else None
            effort = str(route.effort or "") if old["kind"] != "muse" else None
        finally:
            await service._close(db)
        return await self.prepare_session(
            owner_id=owner_id,
            actor_role=None,
            agent_id=str(old["agent_id"]),
            agent_revision_id=str(old["agent_revision_id"]),
            kind=str(old["kind"]),
            model=model,
            effort=effort,
            workspace_scope=workspace_scope,
            timeout_seconds=timeout_seconds,
            dialogue_idempotency_key=f"delegate-worker:{did}:resume:{max(1, int(control_revision))}",
            project_id=project_id,
            dialogue_id=did,
            budgets=budgets,
        )

    async def start_session(
        self,
        session_id: str,
        *,
        dialogue_id: str,
        instruction: str,
        budgets: Mapping[str, Any] | None,
        workspace_scope: Mapping[str, Any],
        timeout_seconds: int | None,
        attempt: int | None = None,
        _control_revision: int | None = None,
        _db_session: Any = None,
        _trigger_message: Any = None,
    ) -> dict[str, Any]:
        sid = _clean_uuid(session_id, code="worker_session_id_invalid")
        did = _clean_uuid(dialogue_id, code="worker_dialogue_id_invalid")
        service = self._service()
        worker_marker = await self._validate_dialogue_session(
            sid,
            did,
            owner_id=None,
            workspace_scope=workspace_scope,
            _db_session=_db_session,
        )
        normalized_budgets = _normalize_runtime_budgets(budgets)
        attempt_number = 1 if attempt is None else attempt
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number < 1
            or attempt_number > int(normalized_budgets.get("max_attempts", 1))
        ):
            raise DelegateWorkerRuntimeError("worker_attempt_budget_exhausted")
        expected_budgets = worker_marker.get("budgets")
        if isinstance(expected_budgets, Mapping) and any(
            int(expected_budgets.get(key) or 0) != int(normalized_budgets.get(key) or 0)
            for key in ("max_tokens", "max_cost_micros", "max_attempts")
        ):
            raise DelegateWorkerRuntimeError("worker_budget_changed")
        expected_timeout = worker_marker.get("timeout_seconds")
        if expected_timeout not in (None, "") and int(expected_timeout) != _bounded_timeout(timeout_seconds):
            raise DelegateWorkerRuntimeError("worker_timeout_changed")
        limit_marker = DelegateWorkerRunLimits(
            {
                "schema_version": 1,
                "dialogue_id": did,
                "workspace_scope_sha256": worker_marker["workspace_scope_sha256"],
                "budgets": normalized_budgets,
                "timeout_seconds": _bounded_timeout(timeout_seconds),
                "attempt": attempt_number,
            }
        )
        if _control_revision is not None and (type(_control_revision) is not int or _control_revision < 1):
            raise DelegateWorkerRuntimeError("worker_control_revision_invalid")
        send_key = (
            f"delegate-worker:{did}:control:{_control_revision}"
            if _control_revision is not None
            else f"delegate-worker:{did}:attempt:{attempt_number}"
        )
        try:
            borrowed_kwargs = (
                {"_db_session": _db_session, "_trigger_message": _trigger_message}
                if _db_session is not None or _trigger_message is not None
                else {}
            )
            result = await service.send(
                sid,
                {
                    "owner_id": worker_marker["owner_id"],
                    "content": str(instruction),
                    "send_key": send_key,
                },
                _delegate_worker_limits=limit_marker,
                **borrowed_kwargs,
            )
        except Exception as exc:
            _raise_runtime_error(exc, fallback="worker_session_start_failed")
        run = result.get("run") if isinstance(result, Mapping) else None
        run = run if isinstance(run, Mapping) else {}
        return {
            "success": True,
            "session_id": sid,
            "dialogue_id": did,
            "run_id": str(result.get("run_id") or "") if isinstance(result, Mapping) else "",
            "work_item_id": str(result.get("work_item_id") or run.get("work_item_id") or "") if isinstance(result, Mapping) else "",
            "message_id": str(run.get("trigger_message_id") or ""),
            "status": str(result.get("status") or "queued") if isinstance(result, Mapping) else "queued",
        }

    async def get_status(self, session_id: str, *, owner_id: str, dialogue_id: str) -> dict[str, Any]:
        sid = _clean_uuid(session_id, code="worker_session_id_invalid")
        did = _clean_uuid(dialogue_id, code="worker_dialogue_id_invalid")
        worker_marker = await self._validate_dialogue_session(
            sid, did, owner_id=owner_id
        )
        service = self._service()
        try:
            projection = await service.get_session(sid, owner_id=str(owner_id))
        except Exception as exc:
            _raise_runtime_error(exc, fallback="worker_status_failed")
        result = (
            _extract_worker_result(
                projection, worker_marker.get("workspace_scope")
            )
            if isinstance(projection, Mapping)
            else None
        )
        if (
            result is not None
            and result.get("assistant_text") is None
            and _worker_local_session_is_terminal(projection)
        ):
            assistant_text = await self._read_persisted_worker_output(
                projection,
                session_id=sid,
                owner_id=str(owner_id),
                kind=str(worker_marker.get("kind") or ""),
            )
            safe_text = _safe_worker_result_text(
                assistant_text, limit=_MAX_RESULT_TEXT
            )
            if safe_text is not None:
                result["assistant_text"] = safe_text
                result["summary"] = safe_text.strip()[:3000]
        return {
            "success": True,
            "dialogue_id": did,
            "session_id": sid,
            "status": str(projection.get("status") or "unknown"),
            "run_id": str((projection.get("run") or {}).get("id") or "") if isinstance(projection.get("run"), Mapping) else None,
            "work_item_id": str((projection.get("work") or {}).get("id") or "") if isinstance(projection.get("work"), Mapping) else None,
            "result": result,
            "turn_status": projection.get("turn_status"),
            "cancel_semantics": projection.get("cancel_semantics"),
        }

    async def _read_persisted_worker_output(
        self,
        projection: Mapping[str, Any],
        *,
        session_id: str,
        owner_id: str,
        kind: str,
    ) -> str | None:
        """Read the transcript row only after its Run/Work receipt settled."""

        run_projection = (
            projection.get("run") if isinstance(projection.get("run"), Mapping) else {}
        )
        run_result = (
            run_projection.get("result")
            if isinstance(run_projection.get("result"), Mapping)
            else {}
        )
        try:
            run_id = _clean_uuid(
                run_projection.get("id"), code="worker_run_id_invalid"
            )
            message_id = _clean_uuid(
                run_result.get("assistant_message_id"),
                code="worker_result_message_invalid",
            )
        except DelegateWorkerRuntimeError:
            return None
        service = self._service()
        db = await service._session()
        try:
            from ..memory.models import AgentRun, ConversationMessage, ConversationSession

            session = await db.get(ConversationSession, UUID(session_id))
            run = await db.get(AgentRun, UUID(run_id))
            message = await db.get(ConversationMessage, UUID(message_id))
            persisted_run_result = (
                run.result if isinstance(getattr(run, "result", None), Mapping) else {}
            ) if run is not None else {}
            expected_message_key = (
                f"muse-assistant:{run_id}" if kind == "muse" else f"cli-assistant:{run_id}"
            )
            if (
                session is None
                or run is None
                or message is None
                or str(session.user_id or "") != owner_id
                or str(run.user_id or "") != owner_id
                or str(run.session_id or "") != session_id
                or str(run.id) != run_id
                or str(message.session_id or "") != session_id
                or str(message.role or "") != "assistant"
                or str(message.sender_id or "") != str(run.agent_id or "")
                or str(message.client_message_id or "") != expected_message_key
                or str(persisted_run_result.get("assistant_message_id") or "") != message_id
            ):
                return None
            return str(message.content or "")[:_MAX_RESULT_TEXT]
        finally:
            await service._close(db)

    async def cancel(self, session_id: str, run_id: str, *, owner_id: str, dialogue_id: str) -> dict[str, Any]:
        sid = _clean_uuid(session_id, code="worker_session_id_invalid")
        rid = _clean_uuid(run_id, code="worker_run_id_invalid")
        did = _clean_uuid(dialogue_id, code="worker_dialogue_id_invalid")
        marker = await self._validate_dialogue_session(sid, did, owner_id=owner_id)
        service = self._service()
        try:
            result = await service.cancel(sid, rid, owner_id=str(owner_id), reason="agent_dialogue_stop")
        except Exception as exc:
            _raise_runtime_error(exc, fallback="worker_cancel_failed")
        return {
            "success": bool(result.get("accepted")) if isinstance(result, Mapping) else False,
            "dialogue_id": did,
            "session_id": sid,
            "worker_kind": marker["kind"],
            "status": str(result.get("status") or "unknown") if isinstance(result, Mapping) else "unknown",
            "cancel_semantics": result.get("cancel_semantics") if isinstance(result, Mapping) else None,
        }

    async def resume(
        self,
        session_id: str,
        *,
        owner_id: str,
        dialogue_id: str,
        instruction: str,
        budgets: Mapping[str, Any] | None,
        workspace_scope: Mapping[str, Any],
        timeout_seconds: int | None,
        control_revision: int,
        attempt: int | None = None,
    ) -> dict[str, Any]:
        sid = _clean_uuid(session_id, code="worker_session_id_invalid")
        did = _clean_uuid(dialogue_id, code="worker_dialogue_id_invalid")
        marker = await self._validate_dialogue_session(sid, did, owner_id=owner_id, workspace_scope=workspace_scope)
        if marker["kind"] == "muse":
            raise DelegateWorkerRuntimeError("muse_remote_resume_unsupported")
        service = self._service()
        normalized_budgets = _normalize_runtime_budgets(budgets)
        attempt_number = 1 if attempt is None else attempt
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or attempt_number < 1
            or attempt_number > int(normalized_budgets.get("max_attempts", 1))
        ):
            raise DelegateWorkerRuntimeError("worker_attempt_budget_exhausted")
        try:
            projection = await service.reconnect(sid, owner_id=str(owner_id))
        except Exception as exc:
            _raise_runtime_error(exc, fallback="worker_resume_state_unavailable")
        if not isinstance(projection, Mapping) or str(projection.get("native_state") or "") != "active":
            raise DelegateWorkerRuntimeError("worker_resume_state_unsafe")
        send_key = f"delegate-worker:{did}:resume:{max(1, int(control_revision))}"
        try:
            result = await service.send(
                sid,
                {"owner_id": str(owner_id), "content": str(instruction), "send_key": send_key},
                _delegate_worker_limits=DelegateWorkerRunLimits(
                    {
                        "schema_version": 1,
                        "dialogue_id": did,
                        "workspace_scope_sha256": marker["workspace_scope_sha256"],
                        "budgets": normalized_budgets,
                        "timeout_seconds": _bounded_timeout(timeout_seconds),
                        "attempt": attempt_number,
                    }
                ),
            )
        except Exception as exc:
            _raise_runtime_error(exc, fallback="worker_resume_failed")
        return {
            "success": True,
            "dialogue_id": did,
            "session_id": sid,
            "run_id": str(result.get("run_id") or "") if isinstance(result, Mapping) else "",
            "work_item_id": str(result.get("work_item_id") or "") if isinstance(result, Mapping) else "",
            "status": str(result.get("status") or "queued") if isinstance(result, Mapping) else "queued",
        }

    async def _validate_dialogue_session(
        self,
        session_id: str,
        dialogue_id: str,
        *,
        owner_id: str | None,
        workspace_scope: Mapping[str, Any] | None = None,
        _db_session: Any = None,
    ) -> dict[str, Any]:
        from ..memory.models import AgentDialogue, AgentRevision, ConversationSession
        from .agent_delegate_policy import (
            revision_has_new_delegate_policy,
            worker_delegate_required_capabilities,
        )

        service = self._service()
        db = _db_session if _db_session is not None else await service._session()
        try:
            session = await db.get(ConversationSession, UUID(session_id))
            dialogue = await db.get(AgentDialogue, UUID(dialogue_id))
            if session is None or dialogue is None:
                raise DelegateWorkerRuntimeError("worker_dialogue_binding_missing")
            if str(dialogue.worker_session_id) != session_id or str(session.user_id) != str(dialogue.owner_user_id):
                raise DelegateWorkerRuntimeError("worker_dialogue_binding_mismatch")
            if owner_id is not None and str(dialogue.owner_user_id) != str(owner_id):
                raise DelegateWorkerRuntimeError("worker_owner_mismatch")
            if str(getattr(session, "agent_id", "") or "") != str(dialogue.worker_agent_id):
                raise DelegateWorkerRuntimeError("worker_agent_binding_mismatch")
            raw_auth = dialogue.authorization_json if isinstance(dialogue.authorization_json, Mapping) else {}
            stored_hash = str(getattr(dialogue, "authorization_hash", "") or "")
            if not stored_hash or stored_hash != _sha256(dict(raw_auth)):
                raise DelegateWorkerRuntimeError("worker_dialogue_authorization_hash_mismatch")
            marker = raw_auth.get("delegate_worker")
            if not isinstance(marker, Mapping):
                raise DelegateWorkerRuntimeError("worker_scope_authorization_missing")
            normalized = dict(marker)
            normalized["owner_id"] = str(dialogue.owner_user_id)
            normalized["agent_id"] = str(dialogue.worker_agent_id)
            normalized["agent_revision_id"] = str(dialogue.worker_agent_revision_id)
            normalized["session_id"] = session_id
            normalized["dialogue_id"] = dialogue_id
            kind = str(normalized.get("kind") or "").strip().casefold()
            if (
                kind not in _KIND_PROVIDER
                or str(self._service()._context(session).get("provider") or "")
                != _KIND_PROVIDER[kind]
            ):
                raise DelegateWorkerRuntimeError("worker_session_kind_mismatch")
            aggregate_budget = raw_auth.get("budget")
            session_budget = marker.get("budgets")
            if isinstance(session_budget, Mapping):
                normalized["budgets"] = {
                    key: int(session_budget[key])
                    for key in ("max_tokens", "max_cost_micros", "max_attempts")
                    if session_budget.get(key) is not None
                }
            elif isinstance(aggregate_budget, Mapping):
                # Compatibility with pre-per-session markers.
                normalized["budgets"] = {
                    key: int(aggregate_budget[key])
                    for key in ("max_tokens", "max_cost_micros", "max_attempts")
                    if aggregate_budget.get(key) is not None
                }
            if isinstance(aggregate_budget, Mapping) and isinstance(
                normalized.get("budgets"), Mapping
            ):
                for key, value in normalized["budgets"].items():
                    aggregate = aggregate_budget.get(key)
                    if aggregate is not None and int(value) > int(aggregate):
                        raise DelegateWorkerRuntimeError("worker_budget_exceeds_dialogue_ceiling")
            expected_scope = normalized.get("workspace_scope")
            if not isinstance(expected_scope, Mapping) or normalized.get("workspace_scope_sha256") != _sha256(dict(expected_scope)):
                raise DelegateWorkerRuntimeError("worker_scope_authorization_invalid")
            revision_row = await db.get(AgentRevision, dialogue.worker_agent_revision_id)
            if (
                revision_row is None
                or not revision_has_new_delegate_policy(revision_row)
            ):
                raise DelegateWorkerRuntimeError("worker_revision_policy_denied")
            expected_native = _resolve_allow_cli_native_tools(
                self.config if self.config is not None else getattr(service, "config", None),
                revision_row,
                kind=str(normalized.get("kind") or ""),
                mode=str(expected_scope.get("mode") or ""),
            )
            if normalized.get("allow_cli_native_tools") is not expected_native:
                raise DelegateWorkerRuntimeError("worker_native_tool_policy_changed")
            raw_revision_caps = getattr(revision_row, "capability_ceiling_json", None)
            if isinstance(raw_revision_caps, Mapping):
                revision_caps = {
                    str(key).strip().casefold()
                    for key, enabled in raw_revision_caps.items()
                    if enabled is True and str(key).strip()
                }
            else:
                revision_caps = {
                    str(item).strip().casefold()
                    for item in (raw_revision_caps or ())
                    if isinstance(item, str) and item.strip()
                }
            expected_capabilities = (
                {"remote_agent_delegate"}
                if kind == "muse"
                else set(
                    worker_delegate_required_capabilities(
                        kind, str(expected_scope.get("mode") or "")
                    )
                )
            )
            if not expected_capabilities <= revision_caps:
                raise DelegateWorkerRuntimeError("worker_revision_scope_capability_missing")
            if workspace_scope is not None:
                supplied = _canonical_scope(workspace_scope, revalidate_current_scope=False)
                if _sha256(supplied) != normalized.get("workspace_scope_sha256"):
                    raise DelegateWorkerRuntimeError("worker_scope_changed")
            session_context = session.context if isinstance(session.context, Mapping) else {}
            from .cli_development_sessions import CLI_DEVELOPMENT_CONTEXT_KEY

            worker_session_marker = session_context.get(CLI_DEVELOPMENT_CONTEXT_KEY)
            worker_session_marker = _read_marker(worker_session_marker) if isinstance(worker_session_marker, Mapping) else None
            if not isinstance(worker_session_marker, Mapping):
                raise DelegateWorkerRuntimeError("worker_session_scope_missing")
            worker_session_capabilities = worker_session_marker.get("capabilities")
            if not isinstance(worker_session_capabilities, list) or {
                str(item).strip().casefold()
                for item in worker_session_capabilities
                if isinstance(item, str) and item.strip()
            } != expected_capabilities:
                raise DelegateWorkerRuntimeError("worker_session_capability_mismatch")
            for key in (
                "dialogue_id",
                "kind",
                "workspace_scope_sha256",
                "allow_cli_native_tools",
                "budgets",
                "timeout_seconds",
            ):
                if key == "budgets":
                    matches = worker_session_marker.get(key) == normalized.get(key)
                else:
                    matches = str(worker_session_marker.get(key) or "") == str(normalized.get(key) or "")
                if not matches:
                    raise DelegateWorkerRuntimeError("worker_session_scope_mismatch")
            if (
                "agent_dialogue_tool_ids" in normalized
                and worker_session_marker.get("agent_dialogue_tool_ids") != []
            ):
                raise DelegateWorkerRuntimeError("worker_nested_dialogue_tools_denied")
            for key in ("owner_id", "agent_id", "agent_revision_id"):
                if str(worker_session_marker.get(key) or "") != str(normalized.get(key) or ""):
                    raise DelegateWorkerRuntimeError("worker_session_binding_mismatch")
            return normalized
        finally:
            if _db_session is None:
                await service._close(db)


def _bounded_timeout(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_TIMEOUT_SECONDS:
        raise DelegateWorkerRuntimeError("worker_timeout_invalid")
    return value


def _normalize_runtime_budgets(value: Mapping[str, Any] | None) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DelegateWorkerRuntimeError("worker_budget_invalid")
    limits = {
        "max_tokens": 1_000_000,
        "max_cost_micros": 1_000_000_000_000,
        "max_attempts": 100,
    }
    result: dict[str, int] = {}
    for key, raw in value.items():
        name = str(key)
        if name in {"max_turns", "max_depth", "cost_currency", "currency"}:
            continue
        if name not in limits or isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= limits[name]:
            raise DelegateWorkerRuntimeError("worker_budget_invalid")
        result[name] = raw
    return result


def _raise_runtime_error(exc: Exception, *, fallback: str) -> None:
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", code):
        code = fallback
    raise DelegateWorkerRuntimeError(code) from None


__all__ = [
    "DelegateWorkerRuntime",
    "DelegateWorkerRuntimeError",
    "DelegateWorkerRunLimits",
    "DelegateWorkerSessionContext",
]
