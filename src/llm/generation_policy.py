"""Provider-independent generation policy definitions."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class GenerationProfile(str, Enum):
    CHAT = "chat"
    ASSISTED_WORK = "assisted_work"
    AUTONOMOUS_WORK = "autonomous_work"
    REVIEW = "review"


class PermissionPolicy(str, Enum):
    CONFIG_DEFAULT = "config_default"
    CONFIRM_MUTATIONS = "confirm_mutations"
    CONFIRM_ALL_TOOLS = "confirm_all_tools"
    AUTO_APPROVE = "auto_approve"


@dataclass(frozen=True)
class GenerationPolicy:
    profile: GenerationProfile
    agentic_completion_enabled: bool
    tool_hints_enabled: bool
    discretionary_tool_loop_enabled: bool
    permission_policy: PermissionPolicy


POLICIES: dict[GenerationProfile, GenerationPolicy] = {
    GenerationProfile.CHAT: GenerationPolicy(
        profile=GenerationProfile.CHAT,
        agentic_completion_enabled=False,
        tool_hints_enabled=True,
        discretionary_tool_loop_enabled=True,
        permission_policy=PermissionPolicy.CONFIRM_MUTATIONS,
    ),
    GenerationProfile.ASSISTED_WORK: GenerationPolicy(
        profile=GenerationProfile.ASSISTED_WORK,
        agentic_completion_enabled=True,
        tool_hints_enabled=True,
        discretionary_tool_loop_enabled=True,
        permission_policy=PermissionPolicy.CONFIRM_ALL_TOOLS,
    ),
    GenerationProfile.AUTONOMOUS_WORK: GenerationPolicy(
        profile=GenerationProfile.AUTONOMOUS_WORK,
        agentic_completion_enabled=True,
        tool_hints_enabled=True,
        discretionary_tool_loop_enabled=True,
        permission_policy=PermissionPolicy.AUTO_APPROVE,
    ),
    GenerationProfile.REVIEW: GenerationPolicy(
        profile=GenerationProfile.REVIEW,
        agentic_completion_enabled=True,
        tool_hints_enabled=True,
        discretionary_tool_loop_enabled=True,
        permission_policy=PermissionPolicy.CONFIRM_ALL_TOOLS,
    ),
}

DEFAULT_GENERATION_PROFILE = GenerationProfile.CHAT
DEFAULT_GENERATION_POLICY = POLICIES[DEFAULT_GENERATION_PROFILE]

_current_generation_policy: contextvars.ContextVar[GenerationPolicy] = (
    contextvars.ContextVar(
        "aoitalk_current_generation_policy",
        default=DEFAULT_GENERATION_POLICY,
    )
)


def resolve_generation_profile(value: Optional[str]) -> GenerationProfile:
    if value is None or value == "":
        return DEFAULT_GENERATION_PROFILE
    try:
        return GenerationProfile(str(value))
    except ValueError as exc:
        allowed = ", ".join(profile.value for profile in GenerationProfile)
        raise ValueError(
            f"Invalid generation profile '{value}'. Allowed values: {allowed}"
        ) from exc


def generation_policy_for_profile(value: Optional[str | GenerationProfile]) -> GenerationPolicy:
    if isinstance(value, GenerationProfile):
        return POLICIES[value]
    return POLICIES[resolve_generation_profile(value)]


def set_current_generation_policy(policy: GenerationPolicy):
    return _current_generation_policy.set(policy)


def reset_current_generation_policy(token) -> None:
    _current_generation_policy.reset(token)


def get_current_generation_policy() -> GenerationPolicy:
    return _current_generation_policy.get()


def get_client_generation_policy(client: object) -> GenerationPolicy:
    policy = getattr(client, "generation_policy", None)
    if isinstance(policy, GenerationPolicy):
        return policy
    return DEFAULT_GENERATION_POLICY
