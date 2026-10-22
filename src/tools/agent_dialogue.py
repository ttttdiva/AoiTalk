"""Generic root-Agent tools for the AoiDots dialogue bridge."""

from __future__ import annotations

import json
from typing import Any

from .core import ToolDefinition, ToolParam


def _json_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


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
        return _json_result({"success": True, "items": result})
    except Exception as exc:
        return _json_result({"success": False, "error": getattr(exc, "code", "agent_worker_list_failed")})


async def _delegate_agent_work(
    title: str,
    instruction: str,
    agent_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    budgets: dict[str, Any] | None = None,
    deadline: str | None = None,
    predecessor_dialogue_id: str | None = None,
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
        )
        return _json_result(result)
    except Exception as exc:
        return _json_result({"success": False, "error": getattr(exc, "code", "agent_work_delegate_failed")})


async def _get_agent_work_status(dialogue_id: str) -> str:
    service = await _service()
    try:
        result = await service.get_agent_work_status(
            dialogue_id,
            actor_user_id=_actor_id(),
        )
        return _json_result({"success": True, "item": result})
    except Exception as exc:
        return _json_result({"success": False, "error": getattr(exc, "code", "agent_work_status_failed")})


async def _send_agent_work_message(dialogue_id: str, message: str) -> str:
    source_run_id = _current_run_id()
    if not source_run_id:
        return _json_result({"success": False, "error": "source_run_required"})
    service = await _service()
    try:
        result = await service.send_agent_work_message(
            dialogue_id,
            message=message,
            source_agent_run_id=source_run_id,
        )
        return _json_result(result)
    except Exception as exc:
        return _json_result({"success": False, "error": getattr(exc, "code", "agent_work_message_failed")})


async def _answer_agent_work_question(dialogue_id: str, input_id: str, answer: str, expected_control_revision: int, expected_input_revision: int) -> str:
    source_run_id = _current_run_id()
    if not source_run_id:
        return _json_result({"success": False, "error": "source_run_required"})
    service = await _service()
    try:
        result = await service.answer_agent_work_question(
            dialogue_id,
            input_id,
            answer=answer,
            source_agent_run_id=source_run_id,
            expected_control_revision=expected_control_revision,
            expected_input_revision=expected_input_revision,
        )
        return _json_result(result)
    except Exception as exc:
        return _json_result({"success": False, "error": getattr(exc, "code", "agent_work_answer_failed")})


async def _escalate_agent_work_question(dialogue_id: str, input_id: str, expected_control_revision: int) -> str:
    service = await _service()
    try:
        result = await service.escalate_agent_work_question(
            dialogue_id,
            input_id,
            expected_control_revision=expected_control_revision,
            source_agent_run_id=_current_run_id(),
        )
        return _json_result(result)
    except Exception as exc:
        return _json_result({"success": False, "error": getattr(exc, "code", "agent_work_question_escalation_failed")})


async def _stop_agent_work(dialogue_id: str, expected_control_revision: int) -> str:
    service = await _service()
    try:
        result = await service.stop_agent_work(
            dialogue_id,
            expected_control_revision=expected_control_revision,
        )
        return _json_result(result)
    except Exception as exc:
        return _json_result({"success": False, "error": getattr(exc, "code", "agent_work_stop_failed")})


async def _resume_agent_work(dialogue_id: str, expected_control_revision: int) -> str:
    service = await _service()
    try:
        result = await service.resume_agent_work(
            dialogue_id,
            expected_control_revision=expected_control_revision,
        )
        return _json_result(result)
    except Exception as exc:
        return _json_result({"success": False, "error": getattr(exc, "code", "agent_work_resume_failed")})


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
            "max_attempts": {"type": "integer", "minimum": 1, "maximum": 100},
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

    definitions = [
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
            name="delegate_agent_work",
            description="Queue one bounded AgentDialogue job for an eligible worker; return only queued references.",
            function=_delegate_agent_work,
            parameters=[
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
            ],
            side_effect="agent_dialogue",
        ),
        _definition(
            name="get_agent_work_status",
            description="Read one AgentDialogue status and safe references without conversation bodies or provider arguments.",
            function=_get_agent_work_status,
            parameters=[ToolParam("dialogue_id", "string", description="AgentDialogue id.")],
            side_effect="none",
        ),
        _definition(
            name="send_agent_work_message",
            description="Send a root/worker Agent message through the ordinary child Chat session; preserve Agent sender provenance.",
            function=_send_agent_work_message,
            parameters=[
                ToolParam("dialogue_id", "string", description="AgentDialogue id."),
                ToolParam("message", "string", description="Bounded message to persist as an encrypted ConversationMessage."),
            ],
            side_effect="agent_dialogue",
        ),
        _definition(
            name="answer_agent_work_question",
            description="Answer a worker's pending question as the root Agent; this never creates human approval.",
            function=_answer_agent_work_question,
            parameters=[
                ToolParam("dialogue_id", "string", description="AgentDialogue id."),
                ToolParam("input_id", "string", description="Pending input id."),
                ToolParam(
                    "answer",
                    "string",
                    description=(
                        "Bounded Agent answer. For choice-bearing questions use the canonical "
                        'JSON string {"choices":[...],"free_text":"..."}.'
                    ),
                ),
                ToolParam("expected_control_revision", "integer", description="Expected current control revision."),
                ToolParam("expected_input_revision", "integer", description="Expected pending Input revision."),
            ],
            side_effect="agent_dialogue",
        ),
        _definition(
            name="escalate_agent_work_question",
            description="Persist a worker Agent question as a human decision request and return its finite Input reference.",
            function=_escalate_agent_work_question,
            parameters=[
                ToolParam("dialogue_id", "string", description="AgentDialogue id."),
                ToolParam("input_id", "string", description="Pending input id."),
                ToolParam("expected_control_revision", "integer", description="Expected current control revision."),
            ],
            side_effect="agent_dialogue",
        ),
        _definition(
            name="stop_agent_work",
            description="Request a bounded stop for one AgentDialogue with a control revision fence.",
            function=_stop_agent_work,
            parameters=[
                ToolParam("dialogue_id", "string", description="AgentDialogue id."),
                ToolParam("expected_control_revision", "integer", description="Expected current control revision."),
            ],
            side_effect="agent_dialogue",
        ),
        _definition(
            name="resume_agent_work",
            description="Request a bounded resume for one non-terminal AgentDialogue with a control revision fence.",
            function=_resume_agent_work,
            parameters=[
                ToolParam("dialogue_id", "string", description="AgentDialogue id."),
                ToolParam("expected_control_revision", "integer", description="Expected current control revision."),
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
