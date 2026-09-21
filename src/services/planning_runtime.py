"""Planning runtime orchestration and human interaction tools."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from collections.abc import Mapping as ABCMapping
import logging
import re
import uuid
from typing import Any, Iterator, Optional

from ..llm.generation_policy import GenerationPolicy, get_current_generation_policy
from ..llm.generation_cancellation import PlanningInteractionTerminated
from ..llm.planning_policy import (
    ApprovedPlan,
    PlanningPolicy,
    PlanningRunPhase,
    PlanningRunState,
    build_planning_system_guidance,
    canonical_digest,
    canonical_plan_digest,
    build_material_action_preview,
    material_action_preview_digest,
    approved_plan_action_allows_tool,
    ATOMIC_APPROVED_PLAN_TOOLS,
    get_current_planning_policy,
    get_current_planning_run_state,
    is_direct_planning_forbidden,
    reset_current_planning_policy,
    reset_current_planning_run_state,
    resolve_planning_policy,
    normalize_evidence_hashes,
    normalize_material_actions,
    validate_material_actions,
    materialize_approved_action_arguments,
    plan_binding_for,
    set_current_planning_policy,
    set_current_planning_run_state,
    should_enter_planning,
)
from ..services.agent_run_service import (
    canonical_audit_digest,
    get_current_agent_run_id,
)
from ..services.turn_context import get_turn_context
from ..services.human_interaction import (
    HumanInteractionKind,
    get_human_interaction_manager,
)
from ..tools.core import ToolDefinition, ToolParam
from ..tools.external_llm_permission import get_permission_request_scope

logger = logging.getLogger(__name__)

PLANNING_HUMAN_INTERACTION_TOOLS = frozenset(
    {"ask_user_question", "submit_plan_for_approval"}
)


@dataclass(frozen=True)
class ApprovedActionDirective:
    """Server-owned instruction for exactly one approved mutation round."""

    index: int
    tool: str
    arguments: dict[str, Any]
    dynamic_fields: tuple[str, ...]
    call_id: str
    plan_id: str
    revision: int
    action_digest: str
    arguments_digest: str


def _planning_failure(
    reason: str,
    user_message: str = "承認済み計画の実行に失敗したため、処理を停止しました。",
) -> PlanningInteractionTerminated:
    state = get_current_planning_run_state()
    if state is not None:
        state.phase = PlanningRunPhase.FAILED
        state.metadata["approved_execution_failure"] = str(reason)
        state.metadata.pop("active_approved_action", None)
    return PlanningInteractionTerminated(
        reason=reason,
        agent_run_status="failed",
        user_message=user_message,
    )


def get_approved_action_directive() -> ApprovedActionDirective | None:
    """Return the next server-owned action, or ``None`` outside execution."""

    state = get_current_planning_run_state()
    if state is None or state.phase != PlanningRunPhase.EXECUTING:
        return None
    plan = state.plan
    if plan is None or not plan.actions:
        raise _planning_failure("structured_actions_required")
    try:
        cursor = int(state.metadata.get("approved_action_cursor", 0))
    except (TypeError, ValueError) as exc:
        raise _planning_failure("approved_action_cursor_invalid") from exc
    if cursor < 0 or cursor > len(plan.actions):
        raise _planning_failure("approved_action_cursor_invalid")
    if cursor == len(plan.actions):
        return None
    action = plan.actions[cursor]
    run_id = str(get_current_agent_run_id() or "").strip()
    if not run_id:
        # Approved mutations are only legal inside the existing durable
        # AgentRun domain.  Failing before provider/tool execution is safer
        # than performing a side effect that cannot receive a receipt.
        raise _planning_failure("approved_action_run_required")
    stable_input = ":".join(
        (
            "aoitalk-approved-plan-action",
            run_id,
            plan.plan_id,
            str(plan.revision),
            plan.canonical_action_digest,
            str(cursor),
        )
    )
    return ApprovedActionDirective(
        index=cursor,
        tool=str(action.get("tool") or ""),
        arguments=dict(action.get("args") or {}),
        dynamic_fields=tuple(action.get("dynamic_fields") or ()),
        call_id=str(uuid.uuid5(uuid.NAMESPACE_URL, stable_input)),
        plan_id=plan.plan_id,
        revision=plan.revision,
        action_digest=plan.canonical_action_digest,
        arguments_digest=canonical_audit_digest(action.get("args") or {}),
    )


def bind_approved_action_call(
    directive: ApprovedActionDirective,
    *,
    tool_name: str,
    proposed_arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate provider intent and return the server-bound arguments."""

    state = get_current_planning_run_state()
    current = get_approved_action_directive()
    if state is None or current != directive:
        raise _planning_failure("approved_action_cursor_changed")
    if str(tool_name or "").strip() != directive.tool:
        raise _planning_failure("approved_action_wrong_tool")
    action = {
        "tool": directive.tool,
        "args": directive.arguments,
        "dynamic_fields": directive.dynamic_fields,
    }
    try:
        bound = materialize_approved_action_arguments(action, proposed_arguments)
    except ValueError as exc:
        raise _planning_failure("approved_action_arguments_rejected") from exc
    if not approved_plan_action_allows_tool(
        state.plan,
        directive.index,
        directive.tool,
        bound,
    ):
        raise _planning_failure("approved_action_policy_mismatch")
    # The atomic Task/Docs lane reads this server-owned metadata while the
    # ToolRouter has the matching TurnContext.tool_call_id bound.  Keep only
    # the already-materialized arguments; provider proposals are never stored.
    state.metadata["active_approved_action"] = {
        "call_id": directive.call_id,
        "tool": directive.tool,
        "arguments": dict(bound),
        "index": directive.index,
        "plan_id": directive.plan_id,
        "revision": directive.revision,
        "action_digest": directive.action_digest,
        "arguments_digest": canonical_audit_digest(bound),
        "directive_arguments_digest": directive.arguments_digest,
    }
    return bound


def get_active_approved_action_execution(
    tool_name: str | None = None,
) -> dict[str, Any] | None:
    """Return the active atomic approved action bound to this tool invocation.

    Task/Docs mutation tools call this helper from inside their normal
    ToolRouter execution.  It fails closed unless the planning state is in the
    executing phase, the cursor still points at the same directive, the
    TurnContext carries that directive's stable call id, and the requested
    tool/arguments match the server-owned action.  Callers receive a defensive
    copy so they cannot mutate planning state.
    """

    state = get_current_planning_run_state()
    if state is None or state.phase != PlanningRunPhase.EXECUTING:
        return None
    metadata = state.metadata.get("active_approved_action")
    if not isinstance(metadata, dict):
        return None
    tool = str(metadata.get("tool") or "").strip()
    if tool not in ATOMIC_APPROVED_PLAN_TOOLS:
        return None
    requested_tool = str(tool_name or "").strip()
    if requested_tool and requested_tool != tool:
        return None
    call_id = str(metadata.get("call_id") or "").strip()
    if not call_id:
        return None
    if str(get_turn_context().tool_call_id or "").strip() != call_id:
        return None
    try:
        index = int(metadata.get("index"))
    except (TypeError, ValueError):
        return None
    directive = get_approved_action_directive()
    if directive is None or directive.index != index or directive.call_id != call_id:
        return None
    if directive.tool != tool:
        return None
    arguments = metadata.get("arguments")
    if not isinstance(arguments, dict):
        return None
    if not approved_plan_action_allows_tool(
        state.plan,
        index,
        tool,
        arguments,
    ):
        return None
    return {
        "call_id": call_id,
        "tool": tool,
        "arguments": dict(arguments),
        "index": index,
        "plan_id": str(metadata.get("plan_id") or directive.plan_id),
        "revision": int(metadata.get("revision") or directive.revision),
        "action_digest": str(
            metadata.get("action_digest") or directive.action_digest
        ),
        "arguments_digest": str(
            metadata.get("arguments_digest") or directive.arguments_digest
        ),
        "directive_arguments_digest": str(
            metadata.get("directive_arguments_digest")
            or directive.arguments_digest
        ),
    }


async def get_approved_action_receipt(
    directive: ApprovedActionDirective,
) -> dict[str, Any] | None:
    """Read an existing durable receipt before any repeated side effect."""

    run_id = str(get_current_agent_run_id() or "").strip()
    if not run_id:
        return None
    from .agent_run_service import AgentRunService

    run = await AgentRunService().get_run(run_id, include_tool_calls=True)
    if not isinstance(run, dict):
        return None
    for receipt in run.get("tool_calls") or ():
        if str(receipt.get("tool_call_id") or "") == directive.call_id:
            return dict(receipt)
    return None


async def _mark_execution_completed(state: PlanningRunState) -> None:
    if state.phase == PlanningRunPhase.COMPLETED:
        return
    state.phase = PlanningRunPhase.COMPLETED
    state.metadata["approved_execution_completed"] = True
    await _record_planning_audit_event(
        "plan.execution.completed",
        status="succeeded",
        payload={
            "plan_id": state.plan.plan_id if state.plan else "",
            "revision": state.plan.revision if state.plan else 0,
            "action_count": len(state.plan.actions) if state.plan else 0,
        },
    )


async def accept_approved_action_receipt(
    directive: ApprovedActionDirective,
    receipt: dict[str, Any],
) -> None:
    """Advance the cursor only after a successful matching durable receipt."""

    state = get_current_planning_run_state()
    current = get_approved_action_directive()
    if state is None or current != directive:
        await fail_approved_action(
            directive,
            reason="approved_action_cursor_changed",
        )
    if str(receipt.get("tool_call_id") or "") != directive.call_id:
        await fail_approved_action(
            directive,
            reason="approved_action_receipt_mismatch",
        )
    if str(receipt.get("tool_name") or "") != directive.tool:
        await fail_approved_action(
            directive,
            reason="approved_action_receipt_mismatch",
        )
    receipt_arguments = receipt.get("arguments")
    if not isinstance(receipt_arguments, dict):
        await fail_approved_action(
            directive,
            reason="approved_action_receipt_mismatch",
        )
    receipt_metadata = receipt.get("metadata")
    if not isinstance(receipt_metadata, dict):
        receipt_metadata = receipt.get("result_metadata")
    if not isinstance(receipt_metadata, dict):
        receipt_metadata = {}
    stored_arguments_digest = str(
        receipt_metadata.get("arguments_digest") or ""
    ).strip()
    active_execution = state.metadata.get("active_approved_action")
    if not isinstance(active_execution, dict):
        active_execution = {}
    expected_arguments_digest = str(
        active_execution.get("arguments_digest") or directive.arguments_digest
    ).strip()
    if stored_arguments_digest:
        if stored_arguments_digest != expected_arguments_digest:
            await fail_approved_action(
                directive,
                reason="approved_action_receipt_mismatch",
            )
        if receipt.get("mutation_confirmed") is not True:
            await fail_approved_action(
                directive,
                reason="approved_action_receipt_mismatch",
            )
        # The digest binds the raw server-owned arguments.  Re-running the
        # policy matcher against a redacted row would reject legitimate
        # secret-bearing fixed values, so only validate the digest here.
    elif not approved_plan_action_allows_tool(
        state.plan,
        directive.index,
        directive.tool,
        receipt_arguments,
    ):
        await fail_approved_action(
            directive,
            reason="approved_action_receipt_mismatch",
        )
    if not bool(receipt.get("success")):
        await fail_approved_action(
            directive,
            reason="approved_action_tool_failed",
            detail=str(receipt.get("result") or ""),
        )
    state.metadata["approved_action_cursor"] = directive.index + 1
    state.metadata.pop("active_approved_action", None)
    await _record_planning_audit_event(
        "plan.action.completed",
        status="succeeded",
        payload={
            "plan_id": directive.plan_id,
            "revision": directive.revision,
            "action_index": directive.index,
            "tool": directive.tool,
            "tool_call_id": directive.call_id,
        },
    )
    if state.plan is not None and directive.index + 1 == len(state.plan.actions):
        await _mark_execution_completed(state)


async def persist_and_accept_approved_action_result(
    directive: ApprovedActionDirective,
    *,
    arguments: dict[str, Any],
    result: Any,
    success: bool,
) -> dict[str, Any]:
    """Persist/reuse one receipt, then advance or stop-first on failure."""

    run_id = str(get_current_agent_run_id() or "").strip()
    receipt = await get_approved_action_receipt(directive)
    if receipt is None:
        if not run_id:
            await fail_approved_action(
                directive,
                reason="approved_action_receipt_unavailable",
            )
        from .agent_run_service import AgentRunService

        receipt = await AgentRunService().record_tool_call(
            run_id,
            tool_name=directive.tool,
            arguments=arguments,
            result=result,
            success=bool(success),
            mutation_confirmed=bool(success),
            tool_call_id=directive.call_id,
            metadata={
                "source": "approved_plan_executor",
                "plan_id": directive.plan_id,
                "plan_revision": directive.revision,
                "action_index": directive.index,
                "action_digest": directive.action_digest,
            },
        )
    if not isinstance(receipt, dict):
        await fail_approved_action(
            directive,
            reason="approved_action_receipt_unavailable",
        )
    await accept_approved_action_receipt(directive, receipt)
    return receipt


async def fail_approved_action(
    directive: ApprovedActionDirective | None,
    *,
    reason: str,
    detail: str = "",
) -> None:
    """Record stop-first failure and terminate the provider loop."""

    payload = {
        "reason": reason,
        "action_index": directive.index if directive else None,
        "tool": directive.tool if directive else None,
        "tool_call_id": directive.call_id if directive else None,
        "detail_digest": canonical_digest(str(detail or "")) if detail else None,
    }
    state = get_current_planning_run_state()
    if state is not None:
        state.metadata.pop("active_approved_action", None)
    await _record_planning_audit_event(
        "plan.action.failed",
        status="failed",
        payload=payload,
    )
    await _record_planning_audit_event(
        "plan.execution.failed",
        status="failed",
        payload=payload,
    )
    raise _planning_failure(reason)


async def _record_planning_audit_event(
    event_type: str,
    *,
    payload: dict[str, Any] | None = None,
    status: str | None = None,
    message: str | None = None,
    run_id: str | None = None,
) -> None:
    """Best-effort durable planning audit event.

    HumanInteractionManager also emits generic interaction events.  These
    plan-specific events make feedback/approval/cancel/timeout inspectable
    without requiring consumers to infer intent from a websocket payload.
    """

    resolved_run_id = str(run_id or get_current_agent_run_id() or "").strip()
    if not resolved_run_id:
        return
    try:
        from .agent_run_service import AgentRunService

        await AgentRunService().record_event(
            resolved_run_id,
            event_type,
            status=status,
            message=message,
            payload=payload or {},
        )
    except Exception as exc:
        logger.debug("Planning audit event skipped (%s): %s", event_type, exc)


def resolve_planning_correlation_ids() -> dict[str, str]:
    """Resolve trusted correlation ids for planning interactions."""
    scope_user_id, scope_session_id = get_permission_request_scope()
    return {
        "agent_run_id": str(get_current_agent_run_id() or "").strip(),
        "session_id": str(scope_session_id or "").strip(),
        "user_id": str(scope_user_id or "").strip(),
    }


def _parse_plan_text(
    plan_text: str,
    *,
    revision: int = 1,
    plan_id: str | None = None,
    actions: Any = None,
    context_selection: Any = None,
    evidence_hashes: Any = None,
    allowed_dynamic_fields: Any = None,
    user_feedback: str = "",
) -> ApprovedPlan:
    text = str(plan_text or "").strip()
    objective = text
    constraints: list[str] = []
    approach = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("objective:"):
            objective = stripped.split(":", 1)[1].strip() or objective
        elif stripped.lower().startswith("constraints:"):
            constraints.append(stripped.split(":", 1)[1].strip())
        elif stripped.lower().startswith("approach:"):
            approach = stripped.split(":", 1)[1].strip()
    parsed_actions = actions
    # Permit a compact ``action: {json}`` line in provider-generated plans.
    if parsed_actions in (None, ""):
        inline_actions: list[Any] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.lower().startswith("action:"):
                continue
            raw_action = stripped.split(":", 1)[1].strip()
            try:
                inline_actions.append(__import__("json").loads(raw_action))
            except (TypeError, ValueError):
                # Keep malformed provider text non-material instead of making
                # an otherwise valid approval request crash.
                continue
        parsed_actions = inline_actions or None
    normalized_actions = normalize_material_actions(parsed_actions)
    if allowed_dynamic_fields and normalized_actions:
        dynamic = allowed_dynamic_fields
        if isinstance(dynamic, str):
            dynamic = [item.strip() for item in re.split(r"[,;\s]+", dynamic) if item.strip()]
        if isinstance(dynamic, (list, tuple, set)):
            dynamic_fields = [str(item).strip() for item in dynamic if str(item).strip()]
            normalized_actions = tuple(
                {
                    **action,
                    "dynamic_fields": sorted(
                        set(action.get("dynamic_fields") or ()) | set(dynamic_fields)
                    ),
                }
                for action in normalized_actions
            )
    return ApprovedPlan(
        plan_id=plan_id or str(uuid.uuid4()),
        revision=max(1, int(revision or 1)),
        objective=objective,
        constraints=tuple(item for item in constraints if item),
        approach=approach,
        raw_text=text,
        actions=normalized_actions,
        context_selection=(
            dict(context_selection) if isinstance(context_selection, ABCMapping) else {}
        ),
        evidence_hashes=normalize_evidence_hashes(evidence_hashes),
        user_feedback=str(user_feedback or ""),
    )


def _redact_plan_display(value: Any) -> Any:
    """Return a secret-redacted plan display value or fail closed.

    Plan text and summaries remain server-owned candidate data.  This helper is
    used only for the human-facing interaction envelope/audit projection; the
    caller keeps the original candidate and its digests unchanged for approval
    authority.  Importing lazily avoids a module cycle and keeps failures
    explicit instead of ever falling back to the raw value.
    """

    try:
        from .outbound_privacy_service import redact_secret_for_local_display
    except Exception as exc:  # pragma: no cover - exercised by boundary tests
        raise ValueError("plan_display_redaction_unavailable") from exc
    try:
        redacted = redact_secret_for_local_display(value)
    except Exception as exc:  # noqa: BLE001 - display redaction is a trust boundary
        raise ValueError("plan_display_redaction_failed") from exc
    if isinstance(value, str) and not isinstance(redacted, str):
        raise ValueError("plan_display_redaction_failed")
    return redacted


async def request_plan_approval(
    *,
    plan_text: str,
    summary: str = "",
    agent_run_id: str = "",
    session_id: str = "",
    user_id: str = "",
    revision: int = 1,
    plan: ApprovedPlan | None = None,
    actions: Any = None,
    context_selection: Any = None,
    evidence_hashes: Any = None,
) -> dict[str, Any] | None:
    manager = get_human_interaction_manager()
    if manager is None:
        return None
    state = get_current_planning_run_state()
    if state is not None:
        state.phase = PlanningRunPhase.AWAITING_PLAN_APPROVAL
    candidate = plan or _parse_plan_text(
        plan_text,
        revision=revision,
        actions=actions,
        context_selection=context_selection,
        evidence_hashes=evidence_hashes,
    )
    if any(
        str(action.get("tool") or "").strip() not in ATOMIC_APPROVED_PLAN_TOOLS
        for action in candidate.actions
    ):
        # This function is also a public compatibility entrypoint, so retain
        # a fail-closed result rather than allowing a direct caller to bypass
        # submit_plan_for_approval_tool's atomicity gate.
        await _record_planning_audit_event(
            "plan.approved_action_atomicity_unsupported",
            status="rejected",
            payload={"tool_count": len(candidate.actions)},
        )
        return None
    try:
        preview = build_material_action_preview(candidate.actions)
    except ValueError as exc:
        await _record_planning_audit_event(
            "plan.material_action_preview_rejected",
            status="rejected",
            payload={"reason": str(exc)[:256]},
        )
        return None
    preview_digest = material_action_preview_digest(preview)
    binding = plan_binding_for(candidate)
    display_summary = str(summary or "実行前に計画を確認してください。").strip()
    try:
        display_plan_text = _redact_plan_display(candidate.raw_text)
        display_summary = _redact_plan_display(display_summary)
        # ``build_material_action_preview`` already applies the display
        # redactor to each argument.  Redact the complete projection again at
        # this boundary so a future preview implementation cannot bypass the
        # plan interaction's privacy contract.
        display_preview = _redact_plan_display(preview)
        if not isinstance(display_preview, dict):
            raise ValueError("plan_display_redaction_failed")
    except ValueError as exc:
        await _record_planning_audit_event(
            "plan.display_redaction_rejected",
            status="rejected",
            payload={"reason": str(exc)},
        )
        return None
    await _record_planning_audit_event(
        "plan.requested",
        status="awaiting_plan_approval",
        payload={
            "plan_binding": binding,
            "revision": candidate.revision,
            # Only the redacted human-facing summary may enter the audit
            # projection.  Raw plan text/candidate values stay server-side.
            "summary": str(display_summary)[:2000],
        },
    )
    return await manager.request_interaction(
        kind=HumanInteractionKind.PLAN_APPROVAL,
        payload={
            "plan_text": display_plan_text,
            "summary": display_summary,
            "actions": ["approve", "feedback", "cancel"],
            "plan_revision": candidate.revision,
            "plan_binding": binding,
            # The material action preview is the only action-shaped data sent
            # to the UI.  The nested binding intentionally carries digests
            # only; raw arguments remain server-owned.
            "material_action_preview": display_preview,
            "material_action_preview_digest": preview_digest,
            "plan_digest": binding["plan_digest"],
            "action_digest": binding["action_digest"],
            "context_selection_hash": binding["context_selection_hash"],
            "plan_revision": binding["revision"],
        },
        agent_run_id=agent_run_id,
        session_id=session_id,
        user_id=user_id,
        revision=revision,
    )


async def execute_ask_user_question(
    *,
    question: str,
    input_type: str = "free_text",
    choices: list[str] | None = None,
    allow_multiple: bool = False,
    allow_free_text: bool = True,
    agent_run_id: str = "",
    session_id: str = "",
    user_id: str = "",
    revision: int = 0,
) -> dict[str, Any]:
    manager = get_human_interaction_manager()
    if manager is None:
        return {"success": False, "error": "human_interaction_unavailable"}
    state = get_current_planning_run_state()
    previous_phase = state.phase if state is not None else None
    if state is not None:
        state.metadata["phase_before_interaction"] = previous_phase
        state.phase = PlanningRunPhase.AWAITING_USER
        revision = max(int(revision or 0), state.interaction_revision + 1)
        state.interaction_revision = revision
    correlation = resolve_planning_correlation_ids()
    result = await manager.request_interaction(
        kind=HumanInteractionKind.ASK_USER_QUESTION,
        payload={
            "question": question,
            "input_type": input_type,
            "choices": list(choices or []),
            "allow_multiple": allow_multiple,
            "allow_free_text": allow_free_text,
        },
        agent_run_id=correlation["agent_run_id"],
        session_id=correlation["session_id"],
        user_id=correlation["user_id"],
        revision=revision,
    )
    pending_id = str(result.get("pending_interaction_id") or "") if isinstance(result, dict) else ""
    await _record_planning_audit_event(
        "interaction.resolution" if isinstance(result, dict) and not result.get("cancelled") else "interaction.cancelled",
        status="resolved" if isinstance(result, dict) and not result.get("cancelled") else "cancelled",
        payload={
            "pending_interaction_id": pending_id,
            "interaction_kind": HumanInteractionKind.ASK_USER_QUESTION.value,
            "revision": revision,
            "cancelled": bool(isinstance(result, dict) and result.get("cancelled")),
        },
    )
    if state is not None:
        resume_phase = state.metadata.pop("phase_before_interaction", None)
        if isinstance(result, dict) and not result.get("cancelled"):
            if isinstance(resume_phase, PlanningRunPhase):
                state.phase = resume_phase
            elif previous_phase is not None:
                state.phase = previous_phase
        else:
            state.phase = PlanningRunPhase.CANCELLED
    if not isinstance(result, dict):
        return {"success": False, "error": "timeout_or_cancelled"}
    if result.get("cancelled"):
        return {"success": False, "error": "cancelled"}
    return {
        "success": True,
        "answer": result.get("answer"),
        "selected_choices": result.get("selected_choices") or [],
    }


async def _ask_user_question_tool(
    question: str,
    input_type: str = "free_text",
    choices: str = "",
    allow_multiple: bool = False,
    allow_free_text: bool = True,
) -> dict[str, Any]:
    choice_list = [item.strip() for item in re.split(r"[;\n]", choices) if item.strip()]
    normalized_type = str(input_type or "free_text").strip().lower()
    if normalized_type == "yes_no":
        choice_list = ["Yes", "No"]
        allow_multiple = False
        allow_free_text = False
    return await execute_ask_user_question(
        question=question,
        input_type=normalized_type,
        choices=choice_list,
        allow_multiple=bool(allow_multiple),
        allow_free_text=bool(allow_free_text),
    )


ASK_USER_QUESTION_TOOL = ToolDefinition(
    name="ask_user_question",
    description=(
        "Ask the user a structured question and wait for a correlated answer. "
        "Use for ambiguity, preference, or approval checkpoints. "
        "Subagents must escalate to the root agent instead of calling this directly."
    ),
    function=_ask_user_question_tool,
    parameters=[
        ToolParam(
            name="question",
            type="string",
            description="Question to present to the user.",
        ),
        ToolParam(
            name="input_type",
            type="string",
            description="One of free_text, single_choice, multi_choice, yes_no, choices_with_free_text.",
            required=False,
            default="free_text",
        ),
        ToolParam(
            name="choices",
            type="string",
            description="Semicolon or newline separated choices when input_type requires choices.",
            required=False,
            default="",
        ),
        ToolParam(
            name="allow_multiple",
            type="boolean",
            description="Allow multiple selections for choice-based questions.",
            required=False,
            default=False,
        ),
        ToolParam(
            name="allow_free_text",
            type="boolean",
            description="Allow additional free-text input alongside choices.",
            required=False,
            default=True,
        ),
    ],
    is_async=True,
    risk="low",
    side_effect="none",
    requires_approval=False,
    owner="planning",
)


async def submit_plan_for_approval_tool(
    plan_text: str,
    summary: str = "",
    actions_json: Any = "",
    context_selection: Any = None,
    evidence_hashes: Any = None,
    allowed_dynamic_fields: Any = None,
) -> dict[str, Any]:
    state = get_current_planning_run_state()
    if state is None:
        return {"success": False, "error": "planning_not_active"}

    if state.phase == PlanningRunPhase.AWAITING_PLAN_APPROVAL:
        return {"success": False, "error": "approval_already_pending"}
    if state.metadata.get("approval_request_active"):
        return {"success": False, "error": "approval_already_pending"}

    try:
        validated_actions = validate_material_actions(actions_json)
    except ValueError as exc:
        await _record_planning_audit_event(
            "plan.structured_actions_required",
            status="rejected",
            payload={"reason": str(exc)},
        )
        return {
            "success": False,
            "error": "structured_actions_required",
            "message": (
                "Plan approval requires a non-empty typed actions_json array "
                "with tool and args for every action."
            ),
        }
    unsupported_tools = sorted(
        {
            str(action.get("tool") or "").strip()
            for action in validated_actions
            if str(action.get("tool") or "").strip()
            not in ATOMIC_APPROVED_PLAN_TOOLS
        }
    )
    if unsupported_tools:
        await _record_planning_audit_event(
            "plan.approved_action_atomicity_unsupported",
            status="rejected",
            payload={"tool_count": len(unsupported_tools)},
        )
        return {
            "success": False,
            "error": "approved_action_atomicity_unsupported",
            "message": "Only atomic approved Task/Docs actions may be planned for execution.",
        }
    try:
        requested_preview = build_material_action_preview(validated_actions)
        requested_preview_digest = material_action_preview_digest(requested_preview)
    except ValueError as exc:
        await _record_planning_audit_event(
            "plan.material_action_preview_rejected",
            status="rejected",
            payload={"reason": str(exc)[:256]},
        )
        return {
            "success": False,
            "error": "material_action_preview_invalid",
            "message": "Approved actions exceed the bounded preview contract.",
        }

    async with state.approval_request_lock:
        if state.phase == PlanningRunPhase.AWAITING_PLAN_APPROVAL:
            return {"success": False, "error": "approval_already_pending"}
        if state.metadata.get("approval_request_active"):
            return {"success": False, "error": "approval_already_pending"}
        state.metadata["approval_request_active"] = True
        try:
            revision = max(1, state.interaction_revision + 1)
            correlation = resolve_planning_correlation_ids()
            previous_plan = state.plan
            candidate = _parse_plan_text(
                plan_text,
                revision=revision,
                plan_id=(previous_plan.plan_id if previous_plan is not None else None),
                actions=validated_actions,
                context_selection=context_selection,
                evidence_hashes=evidence_hashes,
                allowed_dynamic_fields=allowed_dynamic_fields,
                user_feedback=str(state.metadata.get("last_plan_feedback") or ""),
            )
            requested_binding = plan_binding_for(candidate)
            # Recompute from the immutable candidate (rather than trusting any
            # provider/UI value) and retain only the digest in pending state.
            requested_preview = build_material_action_preview(candidate.actions)
            requested_preview_digest = material_action_preview_digest(requested_preview)
            state.metadata["pending_plan_binding"] = requested_binding
            # Reserve the revision before waiting.  Timeout/cancel paths must
            # still advance the monotonic counter so a delayed response cannot
            # be accepted by the next approval request.
            state.interaction_revision = revision
            approval = await request_plan_approval(
                plan_text=plan_text,
                summary=summary,
                agent_run_id=correlation["agent_run_id"],
                session_id=correlation["session_id"],
                user_id=correlation["user_id"],
                revision=revision,
                plan=candidate,
            )
            if not isinstance(approval, dict):
                state.phase = PlanningRunPhase.CANCELLED
                state.metadata["pending_plan_binding"] = requested_binding
                await _record_planning_audit_event(
                    "plan.timeout",
                    status="timeout",
                    payload={"plan_binding": requested_binding, "revision": revision},
                )
                return {"success": False, "error": "timeout_or_cancelled"}

            action = str(approval.get("action") or "").strip().lower()
            if action == "cancel" or approval.get("cancelled"):
                state.phase = PlanningRunPhase.CANCELLED
                state.metadata.pop("pending_plan_binding", None)
                await _record_planning_audit_event(
                    "plan.cancelled",
                    status="cancelled",
                    payload={
                        "plan_binding": requested_binding,
                        "revision": revision,
                        "pending_interaction_id": approval.get("pending_interaction_id"),
                    },
                )
                return {"success": False, "error": "cancelled", "action": "cancel"}

            if action == "feedback":
                state.interaction_revision = revision
                state.phase = PlanningRunPhase.PLANNING
                feedback = str(
                    approval.get("feedback") or approval.get("plan_text") or ""
                )
                state.metadata["last_plan_feedback"] = feedback
                state.metadata["last_plan_feedback_digest"] = canonical_plan_digest(feedback)
                state.metadata.pop("pending_plan_binding", None)
                await _record_planning_audit_event(
                    "plan.feedback",
                    status="feedback",
                    payload={
                        "plan_binding": requested_binding,
                        "revision": revision,
                        "feedback_digest": canonical_plan_digest(feedback),
                    },
                )
                return {
                    "success": False,
                    "action": "feedback",
                    "feedback": feedback,
                    "message": "Revise the plan and call submit_plan_for_approval again.",
                }

            if action != "approve":
                state.phase = PlanningRunPhase.PLANNING
                state.metadata.pop("pending_plan_binding", None)
                await _record_planning_audit_event(
                    "plan.invalid_action",
                    status="invalid",
                    payload={"plan_binding": requested_binding, "revision": revision, "action": action or "unknown"},
                )
                return {
                    "success": False,
                    "error": "invalid_approval_action",
                    "action": action or "unknown",
                    "message": "Plan approval requires action=approve.",
                }

            # Approval echoes the server-generated digest-only binding and
            # action digests.  Never accept client-supplied raw
            # actions/arguments as authority; the candidate built above
            # remains the only action set that can enter execution.
            response_binding = approval.get("plan_binding")
            response_preview = approval.get("material_action_preview")
            response_preview_digest = str(
                approval.get("material_action_preview_digest") or ""
            ).strip()
            response_plan_digest = str(approval.get("plan_digest") or "").strip()
            response_action_digest = str(approval.get("action_digest") or "").strip()
            response_revision_raw = approval.get("plan_revision")
            if response_revision_raw is None:
                response_revision_raw = approval.get("revision")
            # The response echoes the digest-only binding and its two action
            # digests.  The full preview remains request/display data and is
            # intentionally not required in the response (older clients may
            # not echo large previews); if supplied, it must still match.
            echo_valid = (
                isinstance(response_binding, dict)
                and response_binding == requested_binding
                and response_preview_digest == requested_preview_digest
                and response_action_digest == requested_binding["action_digest"]
            )
            if response_plan_digest:
                echo_valid = echo_valid and response_plan_digest == requested_binding["plan_digest"]
            if response_preview is not None:
                echo_valid = echo_valid and (
                    isinstance(response_preview, dict)
                    and response_preview == requested_preview
                )
            # The plan-text echo is display-only.  It may contain the
            # redacted projection sent to the UI (rather than the raw server
            # candidate), so it is deliberately excluded from the approval
            # authority check.  The immutable revision/digest binding above
            # remains the sole authority; an explicit edited_plan field is
            # still rejected below.
            if "edited_plan" in approval:
                echo_valid = False
            try:
                echo_valid = echo_valid and int(response_revision_raw) == revision
            except (TypeError, ValueError):
                echo_valid = False
            # Raw action arrays are never accepted from the client.  The
            # digest-only binding above is the sole action authority.  A
            # full preview, when supplied for a newer client, is checked as a
            # display echo only and never parsed into execution actions.
            for optional_echo_key in ("actions", "actions_json"):
                if optional_echo_key in approval:
                    echo_valid = False
            if not echo_valid:
                state.phase = PlanningRunPhase.PLANNING
                state.metadata.pop("pending_plan_binding", None)
                await _record_planning_audit_event(
                    "plan.binding_mismatch",
                    status="rejected",
                    payload={
                        "plan_binding": requested_binding,
                        "revision": revision,
                        "reason": "approval_echo_mismatch",
                    },
                )
                return {
                    "success": False,
                    "error": "approval_binding_mismatch",
                    "message": "Approval must echo the current preview, binding, digests, and revision exactly.",
                }
            # Preserve the server-owned candidate exactly; a response's plan
            # text is display metadata and cannot alter the approved action.
            approved_plan = candidate
            approved_binding = requested_binding
            state.plan = approved_plan
            state.phase = PlanningRunPhase.EXECUTING
            state.interaction_revision = revision
            state.metadata["approved_plan_binding"] = approved_binding
            state.metadata["approved_action_cursor"] = 0
            state.metadata["approved_execution_completed"] = False
            state.metadata.pop("pending_plan_binding", None)
            await _record_planning_audit_event(
                "plan.approved",
                status="approved",
                payload={
                    "plan_binding": approved_binding,
                    "revision": revision,
                    "pending_interaction_id": approval.get("pending_interaction_id"),
                },
            )
            return {
                "success": True,
                "action": "approve",
                "plan_id": approved_plan.plan_id,
                "revision": approved_plan.revision,
                "plan_binding": approved_binding,
                "material_action_preview": requested_preview,
                "material_action_preview_digest": requested_preview_digest,
                "plan_digest": requested_binding["plan_digest"],
                "action_digest": requested_binding["action_digest"],
                # Return only the safe display projection to the provider/UI;
                # the raw candidate remains in ``state.plan`` for the
                # server-owned executor and is never replaced by this echo.
                "objective": _redact_plan_display(approved_plan.objective),
                "message": (
                    "Plan approved. Proceed with execution using the approved "
                    "goals and constraints."
                ),
            }
        finally:
            state.metadata.pop("approval_request_active", None)


async def _submit_plan_for_approval_entrypoint(
    plan_text: str,
    summary: str = "",
    actions_json: Any = "",
    context_selection: Any = None,
    evidence_hashes: Any = None,
    allowed_dynamic_fields: Any = None,
) -> dict[str, Any]:
    """Provider entrypoint that turns terminal approval outcomes into control flow.

    Keep ``submit_plan_for_approval_tool`` as the compatibility helper used by
    direct callers and existing tests.  Provider tool execution must stop the
    current agentic loop on timeout/cancel rather than feeding a failed tool
    result back to the model for review or continuation.
    """

    result = await submit_plan_for_approval_tool(
        plan_text,
        summary=summary,
        actions_json=actions_json,
        context_selection=context_selection,
        evidence_hashes=evidence_hashes,
        allowed_dynamic_fields=allowed_dynamic_fields,
    )
    if result.get("error") == "timeout_or_cancelled":
        raise PlanningInteractionTerminated(
            reason="plan_approval_timeout",
            agent_run_status="failed",
            user_message=(
                "計画の承認待ちがタイムアウトしたため、実行を終了しました。"
                "変更は実行していません。"
            ),
        )
    if result.get("error") == "cancelled":
        raise PlanningInteractionTerminated(
            reason="plan_approval_cancelled",
            agent_run_status="cancelled",
            user_message="計画の実行をキャンセルしました。変更は実行していません。",
        )
    return result


SUBMIT_PLAN_FOR_APPROVAL_TOOL = ToolDefinition(
    name="submit_plan_for_approval",
    description=(
        "Submit a proposed plan for explicit user approval before executing mutations. "
        "Use only after read-only investigation during planning."
    ),
    function=_submit_plan_for_approval_entrypoint,
    parameters=[
        ToolParam(
            name="plan_text",
            type="string",
            description="Plan text with objective, constraints, and approach.",
        ),
        ToolParam(
            name="summary",
            type="string",
            description="Short summary shown in the approval UI.",
            required=False,
            default="",
        ),
        ToolParam(
            name="actions_json",
            type="array",
            description=(
                "Required ordered list of material actions; each item contains "
                "tool, args, and explicit dynamic_fields."
            ),
            required=True,
            schema={
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "args": {"type": "object"},
                        "dynamic_fields": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["tool", "args"],
                },
            },
        ),
        ToolParam(
            name="context_selection",
            type="object",
            description="Optional hashed context-selection projection bound to approval.",
            required=False,
            default=None,
        ),
        ToolParam(
            name="evidence_hashes",
            type="array",
            description="Optional evidence/reference hashes bound to approval.",
            required=False,
            default=None,
            # Gemini's FunctionDeclaration schema requires an ``items``
            # definition for arrays.  Keep this explicit on the shared
            # ToolDefinition so every provider receives the same, typed
            # contract rather than relying on provider-specific inference.
            schema={"type": "array", "items": {"type": "string"}},
        ),
    ],
    is_async=True,
    risk="low",
    side_effect="none",
    requires_approval=False,
    owner="planning",
)


def create_planning_run_state_if_needed(
    *,
    user_input: str,
    generation_policy: GenerationPolicy,
    planning_policy: PlanningPolicy | None = None,
) -> PlanningRunState | None:
    """Create planning state for a turn without binding ContextVars."""
    policy = planning_policy or get_current_planning_policy()
    if policy == PlanningPolicy.DIRECT:
        return None
    if policy != PlanningPolicy.PLAN_FIRST and not should_enter_planning(
        user_input=user_input,
        generation_policy=generation_policy,
        planning_policy=policy,
    ):
        return None
    return PlanningRunState(phase=PlanningRunPhase.PLANNING)


def build_planning_guidance_for_turn(
    *,
    user_input: str,
    generation_policy: GenerationPolicy,
    planning_policy: PlanningPolicy | None = None,
    planning_state: PlanningRunState | None = None,
) -> str:
    policy = planning_policy or get_current_planning_policy()
    if planning_state is None:
        return build_planning_system_guidance(
            planning_policy=policy,
            generation_policy=generation_policy,
        )
    return build_planning_system_guidance(
        planning_policy=policy,
        generation_policy=generation_policy,
    )


@contextlib.contextmanager
def planning_turn_scope(
    *,
    user_input: str,
    generation_policy: GenerationPolicy,
    planning_policy: PlanningPolicy | str | None = None,
) -> Iterator[PlanningRunState | None]:
    """Bind planning policy/state for one user turn across all LLM providers."""
    policy = resolve_planning_policy(planning_policy)
    policy_token = set_current_planning_policy(policy)
    state = create_planning_run_state_if_needed(
        user_input=user_input,
        generation_policy=generation_policy,
        planning_policy=policy,
    )
    run_token = (
        set_current_planning_run_state(state) if state is not None else None
    )
    try:
        yield state
    finally:
        if run_token is not None:
            reset_current_planning_run_state(run_token)
        reset_current_planning_policy(policy_token)


def initialize_planning_run_state_if_needed(
    *,
    user_input: str,
    generation_policy,
    planning_policy: PlanningPolicy | None = None,
) -> tuple[PlanningRunState | None, str]:
    """Backward-compatible helper; prefer ``planning_turn_scope`` at turn entry."""
    policy = planning_policy or get_current_planning_policy()
    state = create_planning_run_state_if_needed(
        user_input=user_input,
        generation_policy=generation_policy,
        planning_policy=policy,
    )
    return state, build_planning_guidance_for_turn(
        user_input=user_input,
        generation_policy=generation_policy,
        planning_policy=policy,
        planning_state=state,
    )


def get_approved_plan_guidance() -> str:
    state = get_current_planning_run_state()
    if state is None or state.plan is None:
        return ""
    return build_planning_system_guidance(
        planning_policy=get_current_planning_policy(),
        generation_policy=get_current_generation_policy(),
        approved_plan=state.plan,
    )
