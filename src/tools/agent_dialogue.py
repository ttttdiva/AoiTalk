"""Generic root-Agent tools for the AoiDots dialogue bridge."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Annotated, Any, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..api.media_operations_generation_routes import GenerationRequestSpecRequest
from ..services.agent_team_graph_admission import TeamGraphRef

from .core import ToolDefinition, ToolParam


logger = logging.getLogger(__name__)

_ANSWER_WIRE_DESCRIPTION = (
    'For free_text, pass the answer as a plain string. For yes_no, single_choice, '
    'multi_choice, or choices_with_free_text, pass a JSON object encoded as a string '
    'in answer, for example {"choices":["Yes"]}. Use the exact choice strings '
    'offered by the pending Input. Include multiple choices only when allowed. '
    'When free text is allowed, optionally include "free_text": "..."; '
    'for a free-text-only response to a choice question use {"choices":[],"free_text":"..."}.'
)


AGENT_WORK_TOOL_SAFE_ERROR_CODES = frozenset(
    {
        "agent_dialogue_error",
        "agent_dialogue_not_found",
        "agent_dialogue_access_denied",
        "agent_dialogue_stale",
        "agent_dialogue_invalid",
        "agent_control_actor_required",
        "agent_control_agent_mismatch",
        "agent_control_run_invalid",
        "agent_control_revision_invalid",
        "agent_control_work_lease_invalid",
        "agent_answer_bridge_required",
        "agent_answer_relay_conflict",
        "agent_binding_invalid",
        "answer_required",
        "dialogue_answer_choice_not_allowed",
        "dialogue_answer_free_text_invalid",
        "dialogue_answer_invalid",
        "dialogue_answer_json_required",
        "dialogue_answer_multiple_not_allowed",
        "dialogue_answer_required",
        "dialogue_answer_shape_invalid",
        "dialogue_answer_too_long",
        "dialogue_control_action_unavailable",
        "dialogue_continuation_target_invalid",
        "dialogue_continuation_target_missing",
        "dialogue_chain_budget_schema_required",
        "dialogue_control_stale",
        "dialogue_free_text_not_allowed",
        "dialogue_question_choice_invalid",
        "dialogue_question_choices_duplicate",
        "dialogue_question_choices_invalid",
        "dialogue_question_choices_too_many",
        "dialogue_question_flags_invalid",
        "dialogue_question_input_type_invalid",
        "dialogue_terminal",
        "dialogue_yes_no_invalid",
        "human_answer_conflict",
        "human_answer_source_invalid",
        "human_question_requires_user",
        "input_control_revision_stale",
        "input_not_found",
        "input_not_pending",
        "input_revision_stale",
        "question_required",
        "dialogue_id_required",
        "input_id_required",
        "source_run_invalid",
        "source_run_required",
    }
)

_ACTIVE_DIALOGUE_STATUSES = frozenset({"queued", "running", "waiting", "blocked"})
_TERMINAL_DIALOGUE_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "expired"}
)
_DIALOGUE_OPERATION_TOOLS = frozenset(
    {
        "get_agent_work_status",
        "answer_agent_work_question",
        "escalate_agent_work_question",
        "resume_agent_work",
        "send_agent_work_message",
        "ask_agent_work_question",
        "stop_agent_work",
    }
)


def _json_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _safe_agent_work_error_code(exc: Exception) -> str:
    candidate = getattr(exc, "code", None)
    if isinstance(candidate, str) and candidate in AGENT_WORK_TOOL_SAFE_ERROR_CODES:
        return candidate
    logger.warning(
        "agent work tool failed with an unclassified error: %s",
        type(exc).__name__,
        exc_info=exc,
    )
    return "tool_execution_failed"


def _actor_id() -> str:
    from ..services.turn_context import get_turn_context

    value = get_turn_context().user_id
    if not value:
        raise PermissionError("an authenticated human context is required")
    return str(value)


def _current_run_id() -> str | None:
    from ..services.agent_run_service import get_current_agent_run_id
    from ..services.turn_context import get_turn_context

    value = get_current_agent_run_id()
    if value:
        return str(value)
    config = get_turn_context().agent_execution_config
    if isinstance(config, dict):
        candidate = config.get("agent_run_id") or config.get("source_agent_run_id")
        return str(candidate) if candidate else None
    return None


class _DialogueToolResolutionError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _tool_call_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = None
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _tool_call_result(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = None
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _dialogue_id_from_tool_receipt(receipt: Mapping[str, Any]) -> str:
    arguments = _tool_call_arguments(receipt.get("arguments"))
    result = _tool_call_result(receipt.get("result"))
    item = result.get("item") if isinstance(result.get("item"), Mapping) else result
    value = item.get("dialogue_id") if result.get("resolved_ids") is True else None
    value = value or arguments.get("dialogue_id") or item.get("dialogue_id")
    return str(value or "").strip().casefold()


def _status_fingerprint(receipt: Mapping[str, Any]) -> str | None:
    result = _tool_call_result(receipt.get("result"))
    if result.get("unchanged") is True:
        return "unchanged"
    item = result.get("item") if isinstance(result.get("item"), Mapping) else None
    if item is None:
        return None
    status = str(item.get("job_status") or "").casefold()
    if item.get("terminal") is True or status in _TERMINAL_DIALOGUE_STATUSES:
        return None
    inputs = item.get("pending_inputs")
    inputs = inputs if isinstance(inputs, list) else []
    # A pending Worker question is actionable by Root, so repeated status
    # reads must not hide it behind a wait instruction. Human waits already
    # carry the dedicated wait_for_human marker.
    if inputs and result.get("next") != "wait_for_human":
        return None
    input_revisions = sorted(
        (
            str(entry.get("state") or ""),
            int(entry.get("revision") or 0),
            int(entry.get("expected_control_revision") or 0),
        )
        for entry in inputs
        if isinstance(entry, Mapping)
    )
    artifacts = item.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, list) else []
    fingerprint = {
        "status": status,
        "control_revision": int(item.get("control_revision") or 0),
        "current_run_status": item.get("current_run_status"),
        "current_work_status": item.get("current_work_status"),
        "current_delivery_state": item.get("current_delivery_state"),
        "pending_input_count": len(inputs),
        "input_revisions": input_revisions,
        "artifact_count": len(artifacts),
    }
    return json.dumps(
        fingerprint, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


async def _unchanged_status_next_in_current_run(dialogue_id: str) -> str | None:
    run_id = _current_run_id()
    if not run_id or not dialogue_id:
        return None
    try:
        from ..services.agent_run_service import AgentRunService

        run = await AgentRunService().get_run(run_id, include_tool_calls=True)
        receipts = run.get("tool_calls", []) if isinstance(run, Mapping) else []
        if not isinstance(receipts, list):
            return None
        ordered = sorted(
            enumerate(receipts),
            key=lambda pair: (
                str(pair[1].get("created_at") or ""), pair[0]
            )
            if isinstance(pair[1], Mapping)
            else ("", pair[0]),
        )
        matching: list[tuple[str, Mapping[str, Any]]] = []
        for _index, receipt in ordered:
            if not isinstance(receipt, Mapping):
                continue
            name = str(receipt.get("tool_name") or "").strip().rsplit(".", 1)[-1]
            if (
                name in _DIALOGUE_OPERATION_TOOLS
                and _dialogue_id_from_tool_receipt(receipt) == dialogue_id.casefold()
            ):
                matching.append((name, receipt))
        if not matching or matching[-1][0] != "get_agent_work_status":
            return None
        latest_fingerprint = _status_fingerprint(matching[-1][1])
        if latest_fingerprint == "unchanged":
            latest_result = _tool_call_result(matching[-1][1].get("result"))
            return (
                "wait_for_human"
                if latest_result.get("next") == "wait_for_human"
                else "end_turn_and_wait"
            )
        if (
            len(matching) >= 2
            and matching[-2][0] == "get_agent_work_status"
            and latest_fingerprint is not None
            and latest_fingerprint == _status_fingerprint(matching[-2][1])
        ):
            latest_result = _tool_call_result(matching[-1][1].get("result"))
            return (
                "wait_for_human"
                if latest_result.get("next") == "wait_for_human"
                else "end_turn_and_wait"
            )
        return None
    except Exception:
        # A missing/temporarily unavailable audit projection must not block
        # the canonical status tool. Fail open and let the Run receipt path
        # persist this observation normally.
        return None


async def _dialogue_candidates(service: Any) -> list[dict[str, Any]]:
    from ..services.turn_context import get_turn_context

    session_id = get_turn_context().session_id
    list_work = getattr(service, "list_work", None)
    if not session_id or not callable(list_work):
        return []
    candidates: dict[str, dict[str, Any]] = {}
    for state in ("queued", "running", "waiting", "blocked"):
        rows = await list_work(
            actor_user_id=_actor_id(),
            root_session_id=str(session_id),
            state=state,
            limit=100,
        )
        for row in rows if isinstance(rows, list) else ():
            if not isinstance(row, Mapping):
                continue
            candidate_id = str(row.get("dialogue_id") or "").strip()
            status = str(row.get("job_status") or "").casefold()
            if candidate_id and status in _ACTIVE_DIALOGUE_STATUSES:
                candidates[candidate_id.casefold()] = dict(row)
    return list(candidates.values())


def _bound_worker_dialogue_id() -> str | None:
    """A Worker turn is bound to one Dialogue; the model's copy of its id is often wrong."""

    from ..services.turn_context import get_turn_context

    context = get_turn_context()
    if (
        str(context.agent_dialogue_role or "").strip().casefold() == "worker"
        and context.agent_dialogue_id
    ):
        return str(context.agent_dialogue_id)
    return None


async def _resolve_dialogue_id(
    service: Any,
    dialogue_id: str | None,
) -> tuple[str, dict[str, Any] | None, bool]:
    supplied = str(dialogue_id or "").strip()
    get_dialogue = getattr(service, "get_dialogue", None)
    bound_id = _bound_worker_dialogue_id()
    if bound_id and callable(get_dialogue):
        projection = await get_dialogue(bound_id, actor_user_id=_actor_id())
        return (
            bound_id,
            dict(projection) if isinstance(projection, Mapping) else None,
            supplied != bound_id,
        )
    if supplied:
        if not callable(get_dialogue):
            return supplied, None, False
        try:
            projection = await get_dialogue(supplied, actor_user_id=_actor_id())
            return (
                supplied,
                dict(projection) if isinstance(projection, Mapping) else None,
                False,
            )
        except Exception as exc:
            if getattr(exc, "code", None) not in {
                "agent_dialogue_not_found",
                "agent_dialogue_invalid",
            }:
                raise
            candidates = await _dialogue_candidates(service)
            if len(candidates) == 1:
                candidate = candidates[0]
                return str(candidate["dialogue_id"]), candidate, True
            # Preserve the identifier so the canonical action path returns
            # its existing not-found/invalid error unchanged.
            return supplied, None, False

    candidates = await _dialogue_candidates(service)
    if len(candidates) != 1:
        raise _DialogueToolResolutionError("dialogue_id_required")
    candidate = candidates[0]
    return str(candidate["dialogue_id"]), candidate, True


def _resolve_input_id(
    projection: Mapping[str, Any] | None,
    input_id: str | None,
) -> tuple[str, bool]:
    supplied = str(input_id or "").strip()
    pending = projection.get("pending_inputs", []) if isinstance(projection, Mapping) else []
    pending = pending if isinstance(pending, list) else []
    if supplied:
        # A worker that asks again replaces the pending Input, so the id the
        # model remembers is often stale.  When it is not pending any more and
        # exactly one Input is pending, use that one.
        pending_ids = {str(item.get("id")) for item in pending if isinstance(item, Mapping)}
        if supplied not in pending_ids and len(pending) == 1 and isinstance(pending[0], Mapping) and pending[0].get("id"):
            return str(pending[0]["id"]), True
        return supplied, False
    if len(pending) == 1 and isinstance(pending[0], Mapping) and pending[0].get("id"):
        return str(pending[0]["id"]), True
    raise _DialogueToolResolutionError("input_id_required")


def _fresh_control_revision(projection: Mapping[str, Any] | None, supplied: int | None) -> int | None:
    """Root tools always act on the current control revision (the model's copy is often stale)."""

    current = projection.get("control_revision") if isinstance(projection, Mapping) else None
    return current if isinstance(current, int) and not isinstance(current, bool) else supplied


def _fresh_input_revision(
    projection: Mapping[str, Any] | None, input_id: str, supplied: int | None
) -> int | None:
    pending = projection.get("pending_inputs", []) if isinstance(projection, Mapping) else []
    for item in pending if isinstance(pending, list) else ():
        if isinstance(item, Mapping) and str(item.get("id")) == input_id:
            for key in ("expected_input_revision", "revision"):
                value = item.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
    return supplied


def _with_resolved_ids(result: Any, resolved_ids: bool) -> Any:
    if resolved_ids and isinstance(result, Mapping):
        return {**dict(result), "resolved_ids": True}
    return result


async def _service():
    from ..services.agent_dialogue_service import AgentDialogueService, get_agent_dialogue_dispatch_bridge
    from ..services.turn_context import get_turn_context

    config = get_turn_context().agent_execution_config
    bridge = config.get("agent_dialogue_dispatch_bridge") if isinstance(config, dict) else None
    if bridge is None:
        bridge = get_agent_dialogue_dispatch_bridge()
    # The bridge was created with the server's runtime config.  Without it the
    # Worker route preflight sees only the code-owned default Team topology and
    # rejects every saved execution profile as ``agent_route_profile_unavailable``.
    return AgentDialogueService(
        config=getattr(bridge, "config", None),
        dispatch_bridge=bridge,
    )


async def _list_agent_workers(
    project_id: str | None = None,
    root_session_id: str | None = None,
    limit: int = 32,
) -> str:
    service = await _service()
    try:
        result = await service.list_agent_workers(
            actor_user_id=_actor_id(),
            root_session_id=root_session_id,
            project_id=project_id,
            limit=max(1, min(int(limit or 32), 64)),
        )
        try:
            team_graphs = await service.list_team_graph_groups(
                actor_user_id=_actor_id(), project_id=project_id, root_session_id=root_session_id)
            graph_error = None
        except Exception:
            team_graphs, graph_error = [], "team_graph_list_unavailable"
        return _json_result({"success": True, "items": result, "team_graphs": team_graphs,
                             **({"team_graph_error": graph_error} if graph_error else {})})
    except Exception as exc:
        return _json_result({"success": False, "error": getattr(exc, "code", "agent_worker_list_failed")})


async def _delegate(
    title: str,
    instruction: str,
    agent_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    budgets: dict[str, Any] | None = None,
    deadline: str | None = None,
    predecessor_dialogue_id: str | None = None,
    team_graph_ref: dict[str, Any] | None = None,
    *,
    tool_name: str = "delegate",
) -> str:
    service = await _service()
    from ..services.turn_context import get_turn_context

    context_project_hint_id = get_turn_context().context_project_hint_id
    try:
        result = await service.delegate_agent_work(
            actor_user_id=_actor_id(),
            title=title,
            instruction=instruction,
            agent_id=agent_id,
            project_id=project_id,
            task_id=task_id,
            budgets=budgets,
            deadline=deadline,
            context_project_hint_id=context_project_hint_id,
            predecessor_dialogue_id=predecessor_dialogue_id,
            tool_name=tool_name,
            **({"team_graph_ref": team_graph_ref} if team_graph_ref is not None else {}),
        )
        return _json_result(result)
    except Exception as exc:
        return _json_result({"success": False, "error": getattr(exc, "code", "agent_work_delegate_failed")})


async def _delegate_agent_work(
    title: str,
    instruction: str,
    agent_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    budgets: dict[str, Any] | None = None,
    deadline: str | None = None,
    predecessor_dialogue_id: str | None = None,
    team_graph_ref: dict[str, Any] | None = None,
) -> str:
    """Compatibility alias that preserves its exact pinned tool id."""

    return await _delegate(
        title=title,
        instruction=instruction,
        agent_id=agent_id,
        project_id=project_id,
        task_id=task_id,
        budgets=budgets,
        deadline=deadline,
        predecessor_dialogue_id=predecessor_dialogue_id,
        team_graph_ref=team_graph_ref,
        tool_name="delegate_agent_work",
    )


async def _delegate_worker(
    kind: str,
    instruction: str,
    workspace_scope: dict[str, Any],
    budgets: dict[str, Any] | None = None,
    timeout_seconds: int | None = None,
) -> str:
    from ..security.agent_run_scope import AgentRunScope, get_current_run_scope
    from ..services.delegate_worker_arguments import normalize_worker_arguments

    try:
        normalized_kind, normalized_budgets = normalize_worker_arguments(kind, budgets)
        # The model may narrow the scope, but it cannot provide its authority root.
        if not isinstance(workspace_scope, dict) or set(workspace_scope) != {"mode", "paths"}:
            raise ValueError("worker_scope_invalid")
        run_scope = get_current_run_scope()
        if not isinstance(run_scope, AgentRunScope):
            raise ValueError("worker_run_scope_missing")
        scope = {**workspace_scope, "root": str(run_scope.canonical_root)}
        service = await _service()
        result = await service.delegate_worker(
            actor_user_id=_actor_id(),
            kind=normalized_kind,
            instruction=instruction,
            workspace_scope=scope,
            budgets=normalized_budgets,
            timeout_seconds=timeout_seconds,
        )
        return _json_result(result)
    except Exception as exc:
        return _json_result({"success": False, "error": getattr(exc, "code", "agent_work_delegate_failed")})


class DelegateMediaRequestSpec(GenerationRequestSpecRequest):
    @model_validator(mode="after")
    def validate_custom_size(self) -> "DelegateMediaRequestSpec":
        supplied = set(self.model_fields_set)
        super().validate_custom_size()
        self.__pydantic_fields_set__.intersection_update(supplied)
        return self


class DelegateMediaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persona_revision_id: UUID
    creative_recipe_revision_id: UUID
    workspace_id: UUID
    requested_outputs: int = Field(ge=1, le=20, strict=True)
    request_spec: DelegateMediaRequestSpec


async def _delegate_media(
    project_id: UUID,
    team_graph_ref: TeamGraphRef,
    media_request: DelegateMediaRequest,
    idempotency_key: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")],
) -> str:
    from pydantic import TypeAdapter

    from ..services.media_operations_generation_service import (
        MediaOperationsValidationError, _normalize_request_spec,
    )

    try:
        project = TypeAdapter(UUID).validate_python(project_id)
        graph = TeamGraphRef.model_validate(team_graph_ref)
        request = DelegateMediaRequest.model_validate(media_request)
        key = TypeAdapter(Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")]).validate_python(idempotency_key)
        _normalize_request_spec(request.request_spec.model_dump(mode="json", exclude_unset=True))
    except (ValidationError, MediaOperationsValidationError, ValueError, TypeError):
        return _json_result({"success": False, "error": "delegate_media_invalid"})
    try:
        from ..services.agent_team_media_execution import admit_team_media_from_current_agent
    except ImportError:
        return _json_result({"success": False, "error": "delegate_media_service_unavailable"})
    try:
        result = await admit_team_media_from_current_agent(
            project_id=str(project), team_graph_ref=graph.model_dump(mode="json"),
            media_request=request.model_dump(mode="json", exclude_unset=True), idempotency_key=key,
        )
        return _json_result({"success": True, "media": result})
    except Exception as exc:
        logger.exception("agent media delegation failed")
        return _json_result({"success": False, "error": getattr(exc, "code", "tool_execution_failed")})

def _safe_planning_validation_issues(exc):
    """Return bounded field codes without exposing input or dynamic keys."""
    result = {"issue_count": None, "issues": []}
    try:
        from typing import get_args
        from pydantic_core import ErrorType
        from ..services.agent_delegation_planning import DelegationPlanningPolicy

        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        if not isinstance(errors, list):
            return result
        result["issue_count"] = len(errors)
        fields = set(DelegationPlanningPolicy.model_fields)
        error_types = set(get_args(ErrorType))
        for item in errors[:16]:
            if type(item) is not dict:
                continue
            loc = item.get("loc", ())
            if not isinstance(loc, (tuple, list)):
                loc = ()
            safe_loc = []
            for index, part in enumerate(loc[:16]):
                if type(part) is int and 0 <= part <= 1000000:
                    safe_loc.append(part)
                elif index == 0 and isinstance(part, str) and part in fields:
                    safe_loc.append(part)
                else:
                    safe_loc.append("<field>")
            kind = item.get("type")
            if not isinstance(kind, str) or kind not in error_types:
                kind = "unknown"
            result["issues"].append({"loc": safe_loc, "type": kind})
    except Exception:
        return {"issue_count": None, "issues": []}
    return result


async def _delegate_plan(
    title: str, instruction: str, work_role: str, project_id: str, idempotency_key: str,
    worker_agent_id: str | None = None, worker_revision_id: str | None = None,
    reviewer_agent_id: str | None = None, reviewer_revision_id: str | None = None, persona_id: str | None = None,
    planning: dict[str, Any] | None = None, team_graph_ref: dict[str, Any] | None = None,
    media_input_pins: list[dict[str, Any]] | None = None,
    operations_source_ref: dict[str, Any] | None = None,
) -> str:
    from ..services.agent_delegation_service import AgentDelegationService, DelegationError

    phase = "service"
    try:
        dialogue_service = await _service()
        phase = "construct"
        service = AgentDelegationService(dialogue_service._manager(), config=dialogue_service.config)
        phase = "create_from_agent"
        result = await service.create_from_agent(
            title=title, instruction=instruction, work_role=work_role,
            worker_agent_id=worker_agent_id, worker_revision_id=worker_revision_id,
            reviewer_agent_id=reviewer_agent_id, reviewer_revision_id=reviewer_revision_id,
            project_id=project_id, persona_id=persona_id, idempotency_key=idempotency_key,
            planning=planning, team_graph_ref=team_graph_ref, media_input_pins=media_input_pins, operations_source_ref=operations_source_ref,
        )
        return _json_result({"success": True, "delegation": result})
    except DelegationError as exc:
        result = {"success": False, "error": exc.code}
        if exc.code == "delegation_planning_invalid":
            result.update(_safe_planning_validation_issues(getattr(exc, "planning_validation_error", None)))
        return _json_result(result)
    except Exception as exc:
        # Do not log exception text, arguments, locals, or provider payloads.
        # Only fixed project source files can contribute a diagnostic frame.
        frames = []
        tb = exc.__traceback__
        source_root = Path(__file__).resolve().parents[1]
        while tb is not None:
            code = tb.tb_frame.f_code
            try:
                filename = Path(code.co_filename).resolve().relative_to(source_root).as_posix()
            except (OSError, ValueError):
                filename = ""
            if (re.fullmatch(r"[A-Za-z0-9_/.-]+\.py", filename)
                    and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", code.co_name)):
                frames.append(f"{filename}:{code.co_name}:{tb.tb_lineno}")
            tb = tb.tb_next
        error_class = type(exc).__name__
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", error_class):
            error_class = "unclassified"
        logger.error(
            "delegate_plan failure phase=%s error_class=%s frames=%s",
            phase, error_class, ",".join(frames[-8:]),
        )
        return _json_result({"success": False, "error": "tool_execution_failed",
                             "diagnostic": {"phase": phase, "exception_class": error_class,
                                            "frames": frames[-8:]}})


async def _ask_agent_work_question(
    dialogue_id: str | None = None,
    question: str = "",
    input_type: str = "free_text",
    choices: list[str] | None = None,
    allow_multiple: bool = False,
    allow_free_text: bool = True,
    expected_control_revision: int | None = None,
) -> str:
    source_run_id = _current_run_id()
    if not source_run_id:
        return _json_result({"success": False, "error": "source_run_required"})
    from ..services.turn_context import get_turn_context

    context = get_turn_context()
    execution = context.agent_execution_config
    control_revision = expected_control_revision
    if control_revision is None:
        control_revision = context.agent_dialogue_control_revision
    if control_revision is None and isinstance(execution, dict):
        control_revision = execution.get("control_revision")
    service = await _service()
    # The model's copy of the control revision is often stale; use the current one
    # of the Dialogue this worker turn is bound to.
    dialogue_id = _bound_worker_dialogue_id() or dialogue_id
    if not dialogue_id:
        return _json_result({"success": False, "error": "dialogue_id_required"})
    get_dialogue = getattr(service, "get_dialogue", None)
    if callable(get_dialogue):
        try:
            projection = await get_dialogue(dialogue_id, actor_user_id=_actor_id())
            current = (
                projection.get("control_revision")
                if isinstance(projection, Mapping)
                else None
            )
            if isinstance(current, int) and not isinstance(current, bool):
                control_revision = current
        except Exception:
            logger.debug("worker question could not read the current control revision", exc_info=True)
    if control_revision is None:
        return _json_result({"success": False, "error": "dialogue_control_stale"})
    try:
        result = await service.pause_input(
            dialogue_id,
            source_agent_run_id=source_run_id,
            source_work_item_id=(
                execution.get("work_item_id")
                if isinstance(execution, dict)
                else None
            ),
            question=question,
            input_kind="agent_question",
            input_type=input_type,
            choices=choices,
            allow_multiple=allow_multiple,
            allow_free_text=allow_free_text,
            control_revision=int(control_revision),
        )
    except Exception as exc:
        error_code = _safe_agent_work_error_code(exc)
        return _json_result({"success": False, "error": error_code, "error_code": error_code})
    if isinstance(result, Mapping) and result.get("input_id"):
        # The Run, Work and Input are already fenced, so every further tool call
        # would be denied.  End the provider loop here, as ask_user_question does.
        from ..llm.generation_cancellation import AgentDialogueInputPending

        raise AgentDialogueInputPending(
            input_id=str(result["input_id"]),
            dialogue_id=str(result.get("dialogue_id") or dialogue_id),
        )
    return _json_result(result)


async def _delegate_agent_work(
    title: str,
    instruction: str,
    agent_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    budgets: dict[str, Any] | None = None,
    deadline: str | None = None,
    predecessor_dialogue_id: str | None = None,
    team_graph_ref: dict[str, Any] | None = None,
) -> str:
    """Compatibility alias that preserves its exact pinned tool id."""

    return await _delegate(
        title=title,
        instruction=instruction,
        agent_id=agent_id,
        project_id=project_id,
        task_id=task_id,
        budgets=budgets,
        deadline=deadline,
        predecessor_dialogue_id=predecessor_dialogue_id,
        team_graph_ref=team_graph_ref,
        tool_name="delegate_agent_work",
    )


async def _get_agent_work_status(dialogue_id: str | None = None) -> str:
    dialogue_id = _bound_worker_dialogue_id() or dialogue_id
    if not dialogue_id:
        return _json_result({"success": False, "error": "dialogue_id_required"})
    unchanged_next = await _unchanged_status_next_in_current_run(dialogue_id)
    if unchanged_next:
        instruction = (
            "人間の回答が届くまでこの Run を終了して待ってください。"
            if unchanged_next == "wait_for_human"
            else (
                "状態に変化はありません。status を繰り返し呼ばず、"
                "この Run を終了して Worker の event を待ってください。"
            )
        )
        return _json_result(
            {
                "success": True,
                "unchanged": True,
                "next": unchanged_next,
                "instruction": instruction,
            }
        )
    service = await _service()
    try:
        result = await service.get_agent_work_status(
            dialogue_id,
            actor_user_id=_actor_id(),
        )
        if not isinstance(result, Mapping) or result.get("terminal") is True:
            return _json_result({"success": True, "item": result})
        has_pending_human_input = False
        check_human_input = getattr(service, "has_pending_human_input", None)
        if callable(check_human_input):
            has_pending_human_input = bool(
                await check_human_input(
                    dialogue_id,
                    actor_user_id=_actor_id(),
                )
            )
        if has_pending_human_input:
            return _json_result(
                {
                    "success": True,
                    "item": result,
                    "next": "wait_for_human",
                    "instruction": (
                        "人間の回答が届くまでこの Run を終了して待ってください。"
                    ),
                }
            )
        status = str(result.get("job_status") or "").casefold()
        if status not in {"queued", "running", "waiting"}:
            return _json_result({"success": True, "item": result})
        if status in {"queued", "running"} or not result.get("pending_inputs"):
            return _json_result(
                {
                    "success": True,
                    "item": result,
                    "next": "end_turn_and_wait",
                    "instruction": (
                        "Worker の結果は event で届きます。待つために status を"
                        "繰り返し呼ばず、この Run を終了してください。"
                    ),
                }
            )
        return _json_result({"success": True, "item": result})
    except Exception as exc:
        return _json_result({"success": False, "error": getattr(exc, "code", "agent_work_status_failed")})


async def _send_agent_work_message(
    dialogue_id: str | None = None,
    message: str = "",
) -> str:
    source_run_id = _current_run_id()
    if not source_run_id:
        return _json_result(
            {
                "success": False,
                "error": "source_run_required",
                "error_code": "source_run_required",
            }
        )
    service = await _service()
    resolved_ids = False
    try:
        resolved_dialogue_id, _projection, resolved_ids = await _resolve_dialogue_id(
            service, dialogue_id
        )
        result = await service.send_agent_work_message(
            resolved_dialogue_id,
            message=message,
            source_agent_run_id=source_run_id,
        )
        return _json_result(_with_resolved_ids(result, resolved_ids))
    except _DialogueToolResolutionError as exc:
        payload = {"success": False, "error": exc.code, "error_code": exc.code}
        if resolved_ids:
            payload["resolved_ids"] = True
        return _json_result(payload)
    except Exception as exc:
        error_code = _safe_agent_work_error_code(exc)
        payload = {"success": False, "error": error_code, "error_code": error_code}
        if resolved_ids:
            payload["resolved_ids"] = True
        return _json_result(payload)


async def _answer_agent_work_question(
    dialogue_id: str | None = None,
    input_id: str | None = None,
    answer: Annotated[str, Field(description=_ANSWER_WIRE_DESCRIPTION)] = "",
    expected_control_revision: int | None = None,
    expected_input_revision: int | None = None,
) -> str:
    source_run_id = _current_run_id()
    if not source_run_id:
        return _json_result(
            {
                "success": False,
                "error": "source_run_required",
                "error_code": "source_run_required",
            }
        )
    service = await _service()
    resolved_ids = False
    try:
        resolved_dialogue_id, projection, dialogue_resolved = await _resolve_dialogue_id(
            service, dialogue_id
        )
        resolved_ids = dialogue_resolved
        resolved_input_id, input_resolved = _resolve_input_id(projection, input_id)
        resolved_ids = resolved_ids or input_resolved
        result = await service.answer_agent_work_question(
            resolved_dialogue_id,
            resolved_input_id,
            answer=answer,
            source_agent_run_id=source_run_id,
            expected_control_revision=_fresh_control_revision(projection, expected_control_revision),
            expected_input_revision=_fresh_input_revision(
                projection, resolved_input_id, expected_input_revision
            ),
        )
        return _json_result(_with_resolved_ids(result, resolved_ids))
    except _DialogueToolResolutionError as exc:
        payload = {"success": False, "error": exc.code, "error_code": exc.code}
        if resolved_ids:
            payload["resolved_ids"] = True
        return _json_result(payload)
    except Exception as exc:
        error_code = _safe_agent_work_error_code(exc)
        payload = {"success": False, "error": error_code, "error_code": error_code}
        if resolved_ids:
            payload["resolved_ids"] = True
        return _json_result(payload)


async def _escalate_agent_work_question(
    dialogue_id: str | None = None,
    input_id: str | None = None,
    expected_control_revision: int | None = None,
) -> str:
    service = await _service()
    resolved_ids = False
    try:
        resolved_dialogue_id, projection, dialogue_resolved = await _resolve_dialogue_id(
            service, dialogue_id
        )
        resolved_ids = dialogue_resolved
        resolved_input_id, input_resolved = _resolve_input_id(projection, input_id)
        resolved_ids = resolved_ids or input_resolved
        result = await service.escalate_agent_work_question(
            resolved_dialogue_id,
            resolved_input_id,
            expected_control_revision=_fresh_control_revision(projection, expected_control_revision),
            source_agent_run_id=_current_run_id(),
        )
        return _json_result(_with_resolved_ids(result, resolved_ids))
    except _DialogueToolResolutionError as exc:
        payload = {"success": False, "error": exc.code, "error_code": exc.code}
        if resolved_ids:
            payload["resolved_ids"] = True
        return _json_result(payload)
    except Exception as exc:
        error_code = _safe_agent_work_error_code(exc)
        payload = {"success": False, "error": error_code, "error_code": error_code}
        if resolved_ids:
            payload["resolved_ids"] = True
        return _json_result(payload)


async def _stop_agent_work(
    dialogue_id: str | None = None,
    expected_control_revision: int | None = None,
) -> str:
    service = await _service()
    try:
        resolved_dialogue_id, projection, resolved_ids = await _resolve_dialogue_id(
            service, dialogue_id
        )
        result = await service.stop_agent_work(
            resolved_dialogue_id,
            expected_control_revision=_fresh_control_revision(projection, expected_control_revision),
        )
        return _json_result(_with_resolved_ids(result, resolved_ids))
    except _DialogueToolResolutionError as exc:
        return _json_result({"success": False, "error": exc.code, "error_code": exc.code})
    except Exception as exc:
        error_code = _safe_agent_work_error_code(exc)
        return _json_result(
            {"success": False, "error": error_code, "error_code": error_code}
        )


async def _resume_agent_work(
    dialogue_id: str | None = None,
    expected_control_revision: int | None = None,
) -> str:
    service = await _service()
    resolved_ids = False
    try:
        resolved_dialogue_id, projection, resolved_ids = await _resolve_dialogue_id(
            service, dialogue_id
        )
        result = await service.resume_agent_work(
            resolved_dialogue_id,
            expected_control_revision=_fresh_control_revision(projection, expected_control_revision),
        )
        return _json_result(_with_resolved_ids(result, resolved_ids))
    except _DialogueToolResolutionError as exc:
        payload = {"success": False, "error": exc.code, "error_code": exc.code}
        if resolved_ids:
            payload["resolved_ids"] = True
        return _json_result(payload)
    except Exception as exc:
        error_code = _safe_agent_work_error_code(exc)
        payload = {"success": False, "error": error_code, "error_code": error_code}
        if resolved_ids:
            payload["resolved_ids"] = True
        return _json_result(payload)


def _definition(
    *,
    name: str,
    description: str,
    function: Any,
    parameters: list[ToolParam],
    side_effect: str,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        function=function,
        parameters=parameters,
        is_async=True,
        risk="medium" if side_effect != "none" else "low",
        side_effect=side_effect,
        requires_approval=False,
        supports_parallel=False,
        # ``aoi_tools`` is the existing Agent capability family.  The
        # dialogue service performs the stronger per-job/source validation;
        # this avoids inventing a second capability vocabulary in tool_policy.
        owner="aoi_tools",
    )


def build_agent_dialogue_tools() -> list[ToolDefinition]:
    """Return the bounded generic tools exposed to the root Agent."""

    # Keep the nested budget object closed at the model boundary.  The service
    # remains the authority for aliases, range checks, and persisted budget
    # state; this schema only teaches the model the canonical aggregate keys.
    # In particular, a provider per-call field such as ``output_tokens_max``
    # must not be mistaken for the Dialogue's total token ceiling.
    dialogue_budget_schema = {
        "type": "object",
        "properties": {
            "max_turns": {"type": "integer", "minimum": 1, "maximum": 100},
            "max_depth": {"type": "integer", "minimum": 0, "maximum": 32},
            "max_attempts": {"type": "integer", "minimum": 1, "maximum": 16},
            "max_tokens": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1_000_000,
                "description": "Aggregate token ceiling for the whole Dialogue; omit when unset.",
            },
            "max_cost_micros": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1_000_000_000_000,
                "description": "Aggregate cost ceiling in micros; omit when unset.",
            },
            "cost_currency": {
                "type": "string",
                "minLength": 3,
                "maxLength": 4,
                "pattern": "^[A-Za-z0-9]{3,4}$",
                "description": "Currency for max_cost_micros, normally USD.",
            },
        },
        "additionalProperties": False,
    }

    delegate_parameters = [
        ToolParam("title", "string", description="Short bounded work title."),
        ToolParam("instruction", "string", description="The work instruction; stored through ordinary encrypted conversation handling."),
        ToolParam("agent_id", "string", required=False, description="Exact eligible worker Agent id."),
        ToolParam("project_id", "string", required=False, description="Explicit Project scope; a context hint is never attached implicitly."),
        ToolParam("task_id", "string", required=False, description="Optional ACL-checked Task reference."),
        ToolParam(
            "budgets",
            "object",
            required=False,
            default={},
            description=(
                "Aggregate Dialogue ceilings only: max_turns, max_depth, "
                "max_attempts, max_tokens, max_cost_micros, cost_currency. "
                "Do not provide per-call output caps."
            ),
            schema=dialogue_budget_schema,
        ),
        ToolParam("deadline", "string", required=False, description="ISO deadline within the bounded window."),
        ToolParam("predecessor_dialogue_id", "string", required=False, description="Terminal same-owner/same-worker Dialogue to continue with a new pinned Revision."),
        ToolParam("team_graph_ref", "object", required=False, description="Saved graph reference: graph_id, revision_id, and group_key. The group leader is selected by the server."),
    ]
    media_schema = DelegateMediaRequest.model_json_schema()
    # ToolParam schemas are embedded independently, so keep the spec's definition local.
    spec_schema = media_schema.pop("$defs")["DelegateMediaRequestSpec"]
    media_schema["properties"]["request_spec"] = spec_schema

    from ..services.agent_delegation_planning import DelegationPlanningPolicy
    planning_schema = DelegationPlanningPolicy.model_json_schema()
    definitions_by_name = planning_schema.pop("$defs", {})

    def inline_schema(value):
        if isinstance(value, list):
            return [inline_schema(item) for item in value]
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return inline_schema(definitions_by_name[ref.removeprefix("#/$defs/")])
            return {key: inline_schema(item) for key, item in value.items()}
        return value

    planning_schema = inline_schema(planning_schema)
    planning_schema["properties"].pop("graph_constraints", None)
    graph_planning_schema = {**planning_schema, "properties": {k: v for k, v in planning_schema["properties"].items() if k not in {"roster", "graph_constraints"}}}
    graph_planning_schema.pop("required", None)
    definitions = [
        _definition(
            name="delegate_media",
            description="Admit a media request to the saved team graph using the current Agent run.",
            function=_delegate_media,
            parameters=[
                ToolParam("project_id", "string", schema={"type": "string", "format": "uuid"}),
                ToolParam("team_graph_ref", "object", schema=TeamGraphRef.model_json_schema()),
                ToolParam("media_request", "object", schema=media_schema),
                ToolParam("idempotency_key", "string", schema={"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$", "minLength": 1, "maxLength": 255}),
            ],
            side_effect="agent_dialogue",
        ),
        _definition(
            name="delegate_plan",
            description="Create a bounded delegation order with independent worker and reviewer. The current Agent and its active run are verified by the server.",
            function=_delegate_plan,
            parameters=[
                ToolParam("title", "string", description="Short work title."),
                ToolParam("instruction", "string", description="Bounded work instruction."),
                ToolParam("work_role", "string", description="researcher, writer, data_worker, or general_worker."),
                ToolParam("worker_agent_id", "string", required=False),
                ToolParam("worker_revision_id", "string", required=False),
                ToolParam("reviewer_agent_id", "string", required=False),
                ToolParam("reviewer_revision_id", "string", required=False),
                ToolParam("project_id", "string"),
                ToolParam("idempotency_key", "string", description="Stable key for this request."),
                ToolParam("persona_id", "string", required=False),
                ToolParam("planning", "object", required=False, description="Bounded planning options; graph roster and constraints are server supplied. Without a graph, supply the roster.", schema={"anyOf": [graph_planning_schema, planning_schema]}),
                ToolParam("operations_source_ref", "object", required=False, description="Owned current Operations opportunity reference.", schema={"type": "object", "additionalProperties": False, "required": ["opportunity_id", "source_snapshot_hash"], "properties": {"opportunity_id": {"type": "string", "format": "uuid"}, "source_snapshot_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$", "minLength": 64, "maxLength": 64}}}),
                ToolParam("media_input_pins", "array", required=False, description="Up to eight owned image references.", schema={"type": "array", "maxItems": 8, "items": {"type": "object", "additionalProperties": False, "required": ["asset_revision_id", "output_id", "bytes_sha256", "revision_hash", "work_id", "agent_run_id", "plan_id", "plan_hash", "generation_run_id", "generation_run_hash", "output_provenance_hash"], "properties": {**{k: {"type": "string", "format": "uuid"} for k in ("asset_revision_id", "output_id", "work_id", "agent_run_id", "plan_id", "generation_run_id")}, **{k: {"type": "string", "pattern": "^[0-9a-f]{64}$"} for k in ("bytes_sha256", "revision_hash", "plan_hash", "generation_run_hash", "output_provenance_hash")}}}}),
                ToolParam("team_graph_ref", "object", required=False, description="Saved graph reference: graph_id, revision_id, and group_key."),
            ],
            side_effect="agent_dialogue",
        ),
        _definition(
            name="list_agent_workers",
            description="List active Agent workers eligible for the current human/session scope.",
            function=_list_agent_workers,
            parameters=[
                ToolParam("project_id", "string", required=False, description="Explicit ACL-checked Project, when supplied."),
                ToolParam("root_session_id", "string", required=False, description="The current AoiDots root session."),
                ToolParam("limit", "integer", required=False, default=32, description="Maximum workers to list."),
            ],
            side_effect="none",
        ),
        _definition(
            name="delegate",
            description=(
                "Queue one bounded job for an eligible Agent or a saved graph group leader. Use this only when "
                "the current Agent's delegation policy permits that Agent. The worker's "
                "visible tools are limited to the intersection of both Agent revisions; "
                "project access, authority checks, depth, and budget limits are enforced "
                "by the server. Returns a dialogue_id for status and control tools."
            ),
            function=_delegate,
            parameters=delegate_parameters,
            side_effect="agent_dialogue",
        ),
        _definition(
            name="delegate_agent_work",
            description=(
                "Compatibility alias for delegate. Queue one bounded AgentDialogue job "
                "for an eligible worker and return its dialogue_id."
            ),
            function=_delegate_agent_work,
            parameters=delegate_parameters,
            side_effect="agent_dialogue",
        ),
        _definition(
            name="delegate_worker",
            description="Queue bounded work for an authorized external worker kind.",
            function=_delegate_worker,
            parameters=[
                ToolParam("kind", "string", enum=["codex", "antigravity", "claude", "grok", "muse"]),
                ToolParam("instruction", "string", description="Work instruction."),
                ToolParam(
                    "workspace_scope", "object", description="Requested mode and allowed paths within the current run scope.",
                    schema={"type": "object", "properties": {"mode": {"type": "string", "enum": ["read", "write"]}, "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["mode", "paths"], "additionalProperties": False},
                ),
                ToolParam("budgets", "object", required=False, schema=dialogue_budget_schema),
                ToolParam("timeout_seconds", "integer", required=False),
            ],
            side_effect="agent_dialogue",
        ),
        _definition(
            name="ask_agent_work_question",
            description=(
                "Pause the current Worker Run and ask the AoiDots Root a bounded question. "
                "Use this tool whenever you need an answer, a choice or a decision to continue: "
                "a question written as ordinary text ends the work as finished and nobody "
                "answers it. dialogue_id and expected_control_revision are optional; the "
                "server uses this Worker's own Dialogue."
            ),
            function=_ask_agent_work_question,
            parameters=[
                ToolParam("dialogue_id", "string", required=False, description="Optional; the Worker's own Dialogue is used."),
                ToolParam("question", "string", description="A bounded question for the Root Agent."),
                ToolParam("input_type", "string", required=False, default="free_text", description="Question type: free_text, yes_no, or multiple_choice."),
                ToolParam("choices", "array", required=False, description="Choices for multiple_choice questions."),
                ToolParam("allow_multiple", "boolean", required=False, default=False),
                ToolParam("allow_free_text", "boolean", required=False, default=True),
                ToolParam("expected_control_revision", "integer", required=False, description="Current Dialogue control revision."),
            ],
            side_effect="agent_dialogue",
        ),
        _definition(
            name="get_agent_work_status",
            description="Read one AgentDialogue status and safe references without conversation bodies or provider arguments.",
            function=_get_agent_work_status,
            parameters=[
                ToolParam(
                    "dialogue_id",
                    "string",
                    required=False,
                    description="AgentDialogue id. A Worker may omit it; its own Dialogue is used.",
                )
            ],
            side_effect="none",
        ),
        _definition(
            name="send_agent_work_message",
            description=(
                "Send a root/worker Agent message through the ordinary child Chat session "
                "for an active Dialogue; preserve Agent sender provenance. Rework of a "
                "terminal Dialogue is an owner-only control action, not this tool. For a "
                "Root call, omit dialogue_id only when this Root session has exactly one "
                "nonterminal Dialogue; a Worker may omit it only for its bound Dialogue."
            ),
            function=_send_agent_work_message,
            parameters=[
                ToolParam(
                    "dialogue_id",
                    "string",
                    required=False,
                    description=(
                        "AgentDialogue id. Omit only when exactly one nonterminal Dialogue "
                        "matches this Root session (or the Worker is bound to one Dialogue)."
                    ),
                ),
                ToolParam("message", "string", description="Bounded message to persist as an encrypted ConversationMessage."),
            ],
            side_effect="agent_dialogue",
        ),
        _definition(
            name="answer_agent_work_question",
            description=(
                "Answer a worker's pending question as the root Agent; this never creates "
                "human approval. Omit dialogue_id only when this Root session has exactly "
                "one nonterminal Dialogue; omit input_id only when it has exactly one pending Input."
            ),
            function=_answer_agent_work_question,
            parameters=[
                ToolParam(
                    "dialogue_id",
                    "string",
                    required=False,
                    description="Omit only when this Root session has exactly one nonterminal Dialogue.",
                ),
                ToolParam(
                    "input_id",
                    "string",
                    required=False,
                    description="Omit only when this Dialogue has exactly one pending Input.",
                ),
                ToolParam(
                    "answer",
                    "string",
                    description=_ANSWER_WIRE_DESCRIPTION,
                ),
                ToolParam("expected_control_revision", "integer", required=False, description="Optional; the server uses the current control revision."),
                ToolParam("expected_input_revision", "integer", required=False, description="Optional; the server uses the pending Input's revision."),
            ],
            side_effect="agent_dialogue",
        ),
        _definition(
            name="escalate_agent_work_question",
            description=(
                "Persist a worker Agent question as a human decision request and return its "
                "finite Input reference. Omit dialogue_id only when this Root session has "
                "exactly one nonterminal Dialogue; omit input_id only when it has one pending Input."
            ),
            function=_escalate_agent_work_question,
            parameters=[
                ToolParam(
                    "dialogue_id",
                    "string",
                    required=False,
                    description="Omit only when this Root session has exactly one nonterminal Dialogue.",
                ),
                ToolParam(
                    "input_id",
                    "string",
                    required=False,
                    description="Omit only when this Dialogue has exactly one pending Input.",
                ),
                ToolParam("expected_control_revision", "integer", required=False, description="Optional; the server uses the current control revision."),
            ],
            side_effect="agent_dialogue",
        ),
        _definition(
            name="stop_agent_work",
            description=(
                "Request a bounded stop for one AgentDialogue. Omit dialogue_id only when "
                "this Root session has exactly one nonterminal Dialogue."
            ),
            function=_stop_agent_work,
            parameters=[
                ToolParam(
                    "dialogue_id",
                    "string",
                    required=False,
                    description="Omit only when this Root session has exactly one nonterminal Dialogue.",
                ),
                ToolParam("expected_control_revision", "integer", required=False, description="Optional; the server uses the current control revision."),
            ],
            side_effect="agent_dialogue",
        ),
        _definition(
            name="resume_agent_work",
            description=(
                "Request a bounded resume for one non-terminal AgentDialogue with a control "
                "revision fence. Omit dialogue_id only when this Root session has exactly one "
                "nonterminal Dialogue."
            ),
            function=_resume_agent_work,
            parameters=[
                ToolParam(
                    "dialogue_id",
                    "string",
                    required=False,
                    description="Omit only when this Root session has exactly one nonterminal Dialogue.",
                ),
                ToolParam("expected_control_revision", "integer", required=False, description="Optional; the server uses the current control revision."),
            ],
            side_effect="agent_dialogue",
        ),
    ]
    from ..services.turn_context import get_turn_context

    if str(get_turn_context().agent_dialogue_role or "").strip().casefold() == "worker":
        definitions = [
            item
            for item in definitions
            if item.name not in {"stop_agent_work", "resume_agent_work"}
        ]
    return definitions


__all__ = ["build_agent_dialogue_tools"]
