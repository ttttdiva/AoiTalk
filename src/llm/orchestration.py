"""Mode-aware orchestration helpers for multi-step specialist work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .generation_policy import GenerationPolicy, GenerationProfile


WORK_STATE_FIELDS: tuple[str, ...] = (
    "objective",
    "evidence",
    "files_read",
    "facts_found",
    "open_questions",
    "pending_steps",
    "mutation_candidates",
    "verification_results",
)


@dataclass(frozen=True)
class OrchestrationPolicy:
    enabled: bool


def orchestration_policy_for_generation_policy(
    policy: GenerationPolicy,
) -> OrchestrationPolicy:
    """Derive whether multi-step tool guidance should be surfaced to the model."""
    if policy.profile == GenerationProfile.CHAT:
        return OrchestrationPolicy(enabled=True)

    if policy.profile == GenerationProfile.AUTONOMOUS_WORK:
        return OrchestrationPolicy(enabled=True)

    if policy.profile == GenerationProfile.REVIEW:
        return OrchestrationPolicy(enabled=True)

    return OrchestrationPolicy(enabled=policy.agentic_completion_enabled)


def should_add_orchestration_guidance(
    *,
    user_input: str,
    matched_tool_names: Sequence[str],
    policy: GenerationPolicy,
) -> bool:
    orchestration_policy = orchestration_policy_for_generation_policy(policy)
    if not orchestration_policy.enabled:
        return False

    tool_names = {name for name in matched_tool_names if name}
    if len(tool_names) >= 2:
        return True

    text = str(user_input or "").casefold()
    if not text.strip():
        return False

    discovery_terms = (
        "read",
        "inspect",
        "check",
        "look up",
        "compare",
        "verify",
        "調べ",
        "確認",
        "読ん",
        "見て",
        "見直",
        "洗い出",
    )
    follow_up_terms = (
        "then",
        "after",
        "before",
        "once",
        "replan",
        "plan",
        "update",
        "register",
        "apply",
        "その後",
        "した後",
        "必要",
        "追加",
        "更新",
        "登録",
        "反映",
        "修正",
    )
    return any(term in text for term in discovery_terms) and any(
        term in text for term in follow_up_terms
    )


def build_orchestration_guidance(
    *,
    user_input: str,
    matched_tool_names: Sequence[str],
    policy: GenerationPolicy,
) -> str:
    """Build compact instructions for iterative tool orchestration."""
    if not should_add_orchestration_guidance(
        user_input=user_input,
        matched_tool_names=matched_tool_names,
        policy=policy,
    ):
        return ""

    fields = ", ".join(WORK_STATE_FIELDS)
    lines = [
        "## Orchestration Guidance",
        (
            "This request may require iterative tool work, not a one-shot "
            "answer or a single tool call."
        ),
        (
            "Use a work-state loop: plan the next step, call the needed "
            "tool, merge findings, re-plan if new evidence changes the "
            "path, verify, then answer."
        ),
        f"Track these work-state fields: {fields}.",
        (
            "Do not treat the first tool result as final if it raises new "
            "required evidence or follow-up work."
        ),
        (
            "Only claim file reads, searches, database updates, calculations, or "
            "other tool work when successful tool results confirm them."
        ),
    ]

    return "\n".join(lines)
