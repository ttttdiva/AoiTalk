"""Planning runtime orchestration and human interaction tools."""

from __future__ import annotations

import contextlib
import logging
import re
import uuid
from typing import Any, Iterator, Optional

from ..llm.generation_policy import GenerationPolicy, get_current_generation_policy
from ..llm.planning_policy import (
    ApprovedPlan,
    PlanningPolicy,
    PlanningRunPhase,
    PlanningRunState,
    build_planning_system_guidance,
    get_current_planning_policy,
    get_current_planning_run_state,
    is_direct_planning_forbidden,
    reset_current_planning_policy,
    reset_current_planning_run_state,
    resolve_planning_policy,
    set_current_planning_policy,
    set_current_planning_run_state,
    should_enter_planning,
)
from ..services.agent_run_service import get_current_agent_run_id
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


def resolve_planning_correlation_ids() -> dict[str, str]:
    """Resolve trusted correlation ids for planning interactions."""
    scope_user_id, scope_session_id = get_permission_request_scope()
    return {
        "agent_run_id": str(get_current_agent_run_id() or "").strip(),
        "session_id": str(scope_session_id or "").strip(),
        "user_id": str(scope_user_id or "").strip(),
    }


def _parse_plan_text(plan_text: str) -> ApprovedPlan:
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
    return ApprovedPlan(
        plan_id=str(uuid.uuid4()),
        revision=1,
        objective=objective,
        constraints=tuple(item for item in constraints if item),
        approach=approach,
        raw_text=text,
    )


async def request_plan_approval(
    *,
    plan_text: str,
    summary: str = "",
    agent_run_id: str = "",
    session_id: str = "",
    user_id: str = "",
    revision: int = 1,
) -> dict[str, Any] | None:
    manager = get_human_interaction_manager()
    if manager is None:
        return None
    state = get_current_planning_run_state()
    if state is not None:
        state.phase = PlanningRunPhase.AWAITING_PLAN_APPROVAL
    return await manager.request_interaction(
        kind=HumanInteractionKind.PLAN_APPROVAL,
        payload={
            "plan_text": plan_text,
            "summary": summary or "実行前に計画を確認してください。",
            "actions": ["approve", "feedback", "cancel"],
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
) -> dict[str, Any]:
    state = get_current_planning_run_state()
    if state is None:
        return {"success": False, "error": "planning_not_active"}

    if state.phase == PlanningRunPhase.AWAITING_PLAN_APPROVAL:
        return {"success": False, "error": "approval_already_pending"}
    if state.metadata.get("approval_request_active"):
        return {"success": False, "error": "approval_already_pending"}

    async with state.approval_request_lock:
        if state.phase == PlanningRunPhase.AWAITING_PLAN_APPROVAL:
            return {"success": False, "error": "approval_already_pending"}
        if state.metadata.get("approval_request_active"):
            return {"success": False, "error": "approval_already_pending"}
        state.metadata["approval_request_active"] = True
        try:
            revision = state.interaction_revision + 1
            correlation = resolve_planning_correlation_ids()
            approval = await request_plan_approval(
                plan_text=plan_text,
                summary=summary,
                agent_run_id=correlation["agent_run_id"],
                session_id=correlation["session_id"],
                user_id=correlation["user_id"],
                revision=revision,
            )
            if not isinstance(approval, dict):
                state.phase = PlanningRunPhase.CANCELLED
                return {"success": False, "error": "timeout_or_cancelled"}

            action = str(approval.get("action") or "").strip().lower()
            if action == "cancel" or approval.get("cancelled"):
                state.phase = PlanningRunPhase.CANCELLED
                return {"success": False, "error": "cancelled", "action": "cancel"}

            if action == "feedback":
                state.interaction_revision = revision
                state.phase = PlanningRunPhase.PLANNING
                feedback = str(
                    approval.get("feedback") or approval.get("plan_text") or ""
                )
                state.metadata["last_plan_feedback"] = feedback
                return {
                    "success": False,
                    "action": "feedback",
                    "feedback": feedback,
                    "message": "Revise the plan and call submit_plan_for_approval again.",
                }

            if action != "approve":
                state.phase = PlanningRunPhase.PLANNING
                return {
                    "success": False,
                    "error": "invalid_approval_action",
                    "action": action or "unknown",
                    "message": "Plan approval requires action=approve.",
                }

            approved_text = str(
                approval.get("plan_text") or approval.get("edited_plan") or plan_text
            )
            approved_plan = _parse_plan_text(approved_text)
            state.plan = approved_plan
            state.phase = PlanningRunPhase.EXECUTING
            state.interaction_revision = revision
            return {
                "success": True,
                "action": "approve",
                "plan_id": approved_plan.plan_id,
                "objective": approved_plan.objective,
                "message": (
                    "Plan approved. Proceed with execution using the approved "
                    "goals and constraints."
                ),
            }
        finally:
            state.metadata.pop("approval_request_active", None)


SUBMIT_PLAN_FOR_APPROVAL_TOOL = ToolDefinition(
    name="submit_plan_for_approval",
    description=(
        "Submit a proposed plan for explicit user approval before executing mutations. "
        "Use only after read-only investigation during planning."
    ),
    function=submit_plan_for_approval_tool,
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
