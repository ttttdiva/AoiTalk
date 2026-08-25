"""Provider-independent planning policy definitions."""

from __future__ import annotations

import asyncio
import contextvars
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .generation_policy import GenerationPolicy, GenerationProfile


class PlanningPolicy(str, Enum):
    """User-facing planning mode, orthogonal to GenerationProfile."""

    AUTO = "auto"
    PLAN_FIRST = "plan_first"
    DIRECT = "direct"


class PlanningRunPhase(str, Enum):
    """Transient run state while a turn is active."""

    IDLE = "idle"
    PLANNING = "planning"
    AWAITING_USER = "awaiting_user"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ApprovedPlan:
    """Execution contract passed to the normal agentic runtime after approval."""

    plan_id: str
    revision: int
    objective: str
    constraints: tuple[str, ...] = ()
    approach: str = ""
    # Not a script: goals/constraints/policy for the root agent.
    raw_text: str = ""
    user_feedback: str = ""


@dataclass
class PlanningRunState:
    """Mutable planning state for one agent run."""

    phase: PlanningRunPhase = PlanningRunPhase.IDLE
    plan: Optional[ApprovedPlan] = None
    pending_interaction_id: Optional[str] = None
    interaction_revision: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    approval_request_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        repr=False,
    )


DEFAULT_PLANNING_POLICY = PlanningPolicy.AUTO

_current_planning_policy: contextvars.ContextVar[PlanningPolicy] = (
    contextvars.ContextVar(
        "aoitalk_current_planning_policy",
        default=DEFAULT_PLANNING_POLICY,
    )
)
_current_planning_run_state: contextvars.ContextVar[PlanningRunState | None] = (
    contextvars.ContextVar(
        "aoitalk_current_planning_run_state",
        default=None,
    )
)


def resolve_planning_policy(value: Optional[str | PlanningPolicy]) -> PlanningPolicy:
    if isinstance(value, PlanningPolicy):
        return value
    if value is None or str(value).strip() == "":
        return DEFAULT_PLANNING_POLICY
    try:
        return PlanningPolicy(str(value).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in PlanningPolicy)
        raise ValueError(
            f"Invalid planning policy '{value}'. Allowed values: {allowed}"
        ) from exc


def set_current_planning_policy(policy: PlanningPolicy):
    return _current_planning_policy.set(policy)


def reset_current_planning_policy(token) -> None:
    _current_planning_policy.reset(token)


def get_current_planning_policy() -> PlanningPolicy:
    return _current_planning_policy.get()


def set_current_planning_run_state(state: PlanningRunState | None):
    return _current_planning_run_state.set(state)


def reset_current_planning_run_state(token) -> None:
    _current_planning_run_state.reset(token)


def get_current_planning_run_state() -> PlanningRunState | None:
    return _current_planning_run_state.get()


def is_planning_phase_active() -> bool:
    state = get_current_planning_run_state()
    if state is None:
        return False
    return state.phase in {
        PlanningRunPhase.PLANNING,
        PlanningRunPhase.AWAITING_USER,
        PlanningRunPhase.AWAITING_PLAN_APPROVAL,
    }


def is_planning_operator_fanout_forbidden() -> bool:
    """True while a single shared plan/approval gate must cover the whole turn."""
    state = get_current_planning_run_state()
    if state is None:
        return False
    return state.phase in {
        PlanningRunPhase.PLANNING,
        PlanningRunPhase.AWAITING_PLAN_APPROVAL,
    }


def is_planning_cancelled_terminal() -> bool:
    """True when planning was cancelled or timed out and must not resume execution."""
    state = get_current_planning_run_state()
    return state is not None and state.phase == PlanningRunPhase.CANCELLED


def is_direct_planning_forbidden() -> bool:
    return get_current_planning_policy() == PlanningPolicy.DIRECT


_AMBIGUITY_PATTERNS = (
    re.compile(r"\b(or|either|maybe|perhaps|unclear|ambiguous)\b", re.I),
    re.compile(r"(どちら|どっち|どれ|不明|曖昧|迷|未定)"),
)
_CONSEQUENCE_PATTERNS = (
    re.compile(r"\b(delete|drop|deploy|release|production|migrate|refactor)\b", re.I),
    re.compile(r"(削除|本番|リリース|デプロイ|移行|大規模|全面)"),
)
_SCOPE_PATTERNS = (
    re.compile(r"\b(entire|whole|all files|across|multiple modules)\b", re.I),
    re.compile(r"(全体|すべて|複数|横断|一式)"),
)
_PLANNING_COST_LOW_PATTERNS = (
    re.compile(r"\b(fix typo|rename|small|minor|quick)\b", re.I),
    re.compile(r"(タイポ|軽微|ちょっと|少し)"),
)


def should_enter_planning(
    *,
    user_input: str,
    generation_policy: GenerationPolicy,
    planning_policy: PlanningPolicy,
) -> bool:
    """Decide whether to enter a planning phase before agentic execution."""
    if planning_policy == PlanningPolicy.PLAN_FIRST:
        return True
    if planning_policy == PlanningPolicy.DIRECT:
        return False

    text = str(user_input or "").strip()
    if not text:
        return False

    if generation_policy.profile == GenerationProfile.AUTONOMOUS_WORK:
        # autonomous_work should not be stopped for routine complexity alone.
        if any(pattern.search(text) for pattern in _PLANNING_COST_LOW_PATTERNS):
            return False
        if not any(
            pattern.search(text)
            for patterns in (_AMBIGUITY_PATTERNS, _CONSEQUENCE_PATTERNS, _SCOPE_PATTERNS)
            for pattern in patterns
        ):
            return False

    if generation_policy.profile == GenerationProfile.REVIEW:
        return False

    score = 0
    if any(p.search(text) for p in _AMBIGUITY_PATTERNS):
        score += 2
    if any(p.search(text) for p in _CONSEQUENCE_PATTERNS):
        score += 2
    if any(p.search(text) for p in _SCOPE_PATTERNS):
        score += 1
    # Multi-step intent without explicit plan keyword.
    if len(text) > 240:
        score += 1
    if re.search(r"\b(plan|design|architecture|strategy|方針|設計|計画)\b", text, re.I):
        score += 1

    threshold = 3
    if generation_policy.profile == GenerationProfile.CHAT:
        threshold = 4
    return score >= threshold


def build_planning_system_guidance(
    *,
    planning_policy: PlanningPolicy,
    generation_policy: GenerationPolicy,
    approved_plan: ApprovedPlan | None = None,
) -> str:
    """Prompt guidance for planning or post-approval execution."""
    lines = [
        "Planning policy is active for this turn.",
        f"User planning mode: {planning_policy.value}.",
        f"Generation profile: {generation_policy.profile.value}.",
    ]
    if approved_plan is not None:
        lines.extend(
            [
                "An approved plan is in effect. Treat it as goals/constraints, not a rigid script.",
                f"Plan objective: {approved_plan.objective}",
            ]
        )
        if approved_plan.constraints:
            lines.append(
                "Constraints: " + "; ".join(approved_plan.constraints)
            )
        if approved_plan.approach:
            lines.append(f"Approach: {approved_plan.approach}")
        if approved_plan.user_feedback:
            lines.append(f"User feedback on prior plan: {approved_plan.user_feedback}")
    elif is_planning_phase_active():
        lines.append(
            "You are in planning mode. Gather context with read-only tools only. "
            "Do not mutate files, run destructive commands, or cause external side effects. "
            "When ready, produce a concise plan and request plan approval."
        )
    elif planning_policy == PlanningPolicy.DIRECT:
        lines.append(
            "Direct mode: do not initiate voluntary planning phases. "
            "You may still use ask_user_question or await tool permissions when needed."
        )
    return "\n".join(lines)
