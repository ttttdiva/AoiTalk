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
    """ツール実行に確認ダイアログを出す範囲。

    既定は自由側（CONFIRM_DESTRUCTIVE）に倒し、設定で厳しくできるようにする。

    - ``CONFIG_DEFAULT``: 設定ファイルの ``external_llm.auto_approve`` に従う。
    - ``CONFIRM_DESTRUCTIVE``: 取り返しのつかない操作（削除系・破壊的コマンド）だけ確認する。
    - ``CONFIRM_MUTATIONS``: 変更を伴うツール全般で確認する（設定で厳しくする用）。
    - ``CONFIRM_ALL_TOOLS``: 対象ツールすべてで確認する（設定で厳しくする用）。
    - ``AUTO_APPROVE``: 一切確認しない。
    """

    CONFIG_DEFAULT = "config_default"
    CONFIRM_DESTRUCTIVE = "confirm_destructive"
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
    # 既定は「自由」。作成・編集・Docs更新・検索はそのまま実行し、
    # 削除など取り返しのつかない操作だけ確認する。
    GenerationProfile.CHAT: GenerationPolicy(
        profile=GenerationProfile.CHAT,
        agentic_completion_enabled=True,
        tool_hints_enabled=True,
        discretionary_tool_loop_enabled=True,
        permission_policy=PermissionPolicy.CONFIRM_DESTRUCTIVE,
    ),
    GenerationProfile.ASSISTED_WORK: GenerationPolicy(
        profile=GenerationProfile.ASSISTED_WORK,
        agentic_completion_enabled=True,
        tool_hints_enabled=True,
        discretionary_tool_loop_enabled=True,
        permission_policy=PermissionPolicy.CONFIRM_DESTRUCTIVE,
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
        permission_policy=PermissionPolicy.CONFIRM_DESTRUCTIVE,
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


def resolve_permission_policy(value: object) -> Optional[PermissionPolicy]:
    """設定値から ``PermissionPolicy`` を安全に解決する。

    未設定・空文字・未知の文字列は ``None`` を返す。設定ミスで動かなくなるより、
    既定のポリシーで動くほうを優先するため、ここでは例外を投げない。
    """
    if isinstance(value, PermissionPolicy):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return PermissionPolicy(text)
    except ValueError:
        return None


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
