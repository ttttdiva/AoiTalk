"""無料Teamの routing/profile overlay 用の初期値。

Free Team は Agent Team の topology を所有しない。ここで定義するのは、
``routing_profiles.free-team`` が参照する候補プールと、canonical な
``agent_team.llm_profiles`` に対応する pool target だけである。
"""

from __future__ import annotations

import copy
from typing import Any


_PROFILE_TEMPLATE: dict[str, Any] = {
    "display_name": "無料Team",
    "enabled": True,
    "main_pool_id": "coordinator",
    "max_fallbacks": 6,
    "credential_profiles": {
        "openai-complimentary": {
            "display_name": "OpenAI 無料トークン枠",
            "provider": "openai",
            "authentication_type": "api_key",
            "environment_variable": "OPENAI_FREE_TEAM_API_KEY",
            "billing_mode": "complimentary",
            "privacy_class": "standard",
        },
        "gemini-free": {
            "display_name": "Gemini Free Tier",
            "provider": "gemini",
            "authentication_type": "api_key",
            "environment_variable": "GEMINI_FREE_API_KEY",
            "billing_mode": "free_tier",
            "privacy_class": "standard",
        },
        "openrouter-free": {
            "display_name": "OpenRouter Free",
            "provider": "openrouter",
            "authentication_type": "api_key",
            "environment_variable": "OPENROUTER_FREE_API_KEY",
            "base_url": "https://openrouter.ai/api/v1",
            "billing_mode": "free_tier",
            "privacy_class": "standard",
        },
        "gemini-promo": {
            "display_name": "Gemini プロモーションクレジット",
            "provider": "gemini",
            "authentication_type": "api_key",
            "environment_variable": "GEMINI_PROMO_API_KEY",
            "billing_mode": "promo_credit",
            "privacy_class": "standard",
        },
        "codex-spark": {
            "display_name": "Codex CLI / Spark",
            "provider": "codex-cli",
            "authentication_type": "cli",
            "cli_auth_reference": "codex",
            "billing_mode": "subscription_cli",
            "privacy_class": "local_cli",
        },
        "antigravity-cli": {
            "display_name": "Antigravity CLI",
            "provider": "antigravity-cli",
            "authentication_type": "cli",
            "cli_auth_reference": "agy",
            "billing_mode": "subscription_cli",
            "privacy_class": "local_cli",
        },
        "grok-cli": {
            "display_name": "Grok Build CLI",
            "provider": "grok-cli",
            "authentication_type": "cli",
            "cli_auth_reference": "grok",
            "billing_mode": "subscription_cli",
            "privacy_class": "local_cli",
        },
    },
    "quota_pools": {
        "openai-sol-daily": {"credential_profile_id": "openai-complimentary", "metric_type": "total_tokens", "limit": 1_000_000, "safety_margin_ratio": 0.01, "reset_policy": {"kind": "daily", "timezone": "UTC", "hour": 0}},
        "openai-luna-daily": {"credential_profile_id": "openai-complimentary", "metric_type": "total_tokens", "limit": 10_000_000, "safety_margin_ratio": 0.01, "reset_policy": {"kind": "daily", "timezone": "UTC", "hour": 0}},
        "gemini-free-rpm": {"credential_profile_id": "gemini-free", "metric_type": "rpm", "limit": 0, "reset_policy": {"kind": "minute"}},
        "gemini-free-tpm": {"credential_profile_id": "gemini-free", "metric_type": "tpm", "limit": 0, "reset_policy": {"kind": "minute"}},
        "gemini-free-rpd": {"credential_profile_id": "gemini-free", "metric_type": "rpd", "limit": 0, "reset_policy": {"kind": "daily", "timezone": "UTC", "hour": 0}},
        "openrouter-free-rpm": {"credential_profile_id": "openrouter-free", "metric_type": "rpm", "limit": 20, "reset_policy": {"kind": "minute"}},
        "openrouter-free-rpd": {"credential_profile_id": "openrouter-free", "metric_type": "rpd", "limit": 1000, "safety_margin_units": 2, "reset_policy": {"kind": "daily", "timezone": "UTC", "hour": 0}},
        "gemini-promo-monthly-usd": {"credential_profile_id": "gemini-promo", "metric_type": "usd", "limit": 10, "safety_margin_ratio": 0.01, "reset_policy": {"kind": "monthly", "timezone": "UTC", "day": 1, "hour": 0}},
        "codex-spark-local": {"credential_profile_id": "codex-spark", "metric_type": "concurrency", "limit": 1, "reset_policy": {"kind": "none"}},
        "antigravity-local": {"credential_profile_id": "antigravity-cli", "metric_type": "concurrency", "limit": 1, "reset_policy": {"kind": "none"}},
        "grok-local": {"credential_profile_id": "grok-cli", "metric_type": "concurrency", "limit": 1, "reset_policy": {"kind": "none"}},
    },
    "candidates": {
        "openai-luna": {"credential_profile_id": "openai-complimentary", "provider": "openai", "model": "gpt-5.6-luna", "effort": "max", "priority": 10, "quota_pool_ids": ["openai-luna-daily"], "capabilities": ["text", "structured_output", "coding", "long_context"], "quality_class": "heavy", "max_input_tokens": 196000, "max_output_tokens": 4096, "tool_call_policy": {"complimentary_tool_calls_allowed": False}},
        "openai-sol": {"credential_profile_id": "openai-complimentary", "provider": "openai", "model": "gpt-5.6-sol", "effort": "medium", "priority": 11, "quota_pool_ids": ["openai-sol-daily"], "capabilities": ["text", "structured_output", "coding", "long_context"], "quality_class": "heavy", "max_input_tokens": 196000, "max_output_tokens": 4096, "tool_call_policy": {"complimentary_tool_calls_allowed": False}},
        "gemini-free-flash": {"credential_profile_id": "gemini-free", "provider": "gemini", "model": "gemini-2.5-flash", "priority": 20, "quota_pool_ids": ["gemini-free-rpm", "gemini-free-tpm", "gemini-free-rpd"], "capabilities": ["text", "tools", "vision", "structured_output", "coding", "long_context"], "quality_class": "standard", "max_input_tokens": 100000, "max_output_tokens": 4096},
        "gemini-free-lite": {"credential_profile_id": "gemini-free", "provider": "gemini", "model": "gemini-2.5-flash-lite", "priority": 21, "quota_pool_ids": ["gemini-free-rpm", "gemini-free-tpm", "gemini-free-rpd"], "capabilities": ["text", "tools", "structured_output", "long_context"], "quality_class": "light", "max_input_tokens": 100000, "max_output_tokens": 4096},
        "openrouter-free-router": {"credential_profile_id": "openrouter-free", "provider": "openrouter", "model": "openrouter/free", "priority": 30, "quota_pool_ids": ["openrouter-free-rpm", "openrouter-free-rpd"], "capabilities": ["text", "tools", "coding"], "quality_class": "standard", "max_input_tokens": 32000, "max_output_tokens": 2048, "provider_options": {"max_price": 0, "allow_fallbacks": False, "require_free": True}},
        "gemini-promo-flash": {"credential_profile_id": "gemini-promo", "provider": "gemini", "model": "gemini-2.5-flash", "priority": 40, "quota_pool_ids": ["gemini-promo-monthly-usd"], "capabilities": ["text", "tools", "vision", "structured_output", "coding", "long_context"], "quality_class": "standard", "max_input_tokens": 100000, "max_output_tokens": 4096, "provider_options": {"input_price_per_million": 0.30, "output_price_per_million": 2.50}},
        "codex-spark": {"credential_profile_id": "codex-spark", "provider": "codex-cli", "model": "gpt-5.3-codex-spark", "priority": 50, "quota_pool_ids": ["codex-spark-local"], "capabilities": ["text", "tools", "coding", "long_context"], "quality_class": "coding", "max_input_tokens": 100000, "max_output_tokens": 4096},
        "antigravity": {"credential_profile_id": "antigravity-cli", "provider": "antigravity-cli", "model": "default", "priority": 60, "quota_pool_ids": ["antigravity-local"], "capabilities": ["text", "tools", "coding"], "quality_class": "standard", "max_input_tokens": 64000, "max_output_tokens": 4096},
        "grok-build": {"credential_profile_id": "grok-cli", "provider": "grok-cli", "model": "grok-build", "priority": 70, "quota_pool_ids": ["grok-local"], "capabilities": ["text", "tools", "coding"], "quality_class": "unstable", "max_input_tokens": 32000, "max_output_tokens": 2048},
    },
    "pools": {
        "coordinator": {"tool_mode": "auto", "candidate_ids": ["openai-luna", "openai-sol", "gemini-free-flash", "openrouter-free-*", "gemini-promo-flash", "codex-spark", "antigravity", "grok-build"]},
        "heavy": {"tool_mode": "disabled", "candidate_ids": ["openai-sol", "openai-luna", "gemini-free-flash", "openrouter-free-*", "gemini-promo-flash", "codex-spark"]},
        "light": {"tool_mode": "required", "candidate_ids": ["gemini-free-lite", "gemini-free-flash", "openrouter-free-*", "openai-luna", "codex-spark", "antigravity", "grok-build"]},
        "coding": {"tool_mode": "required", "candidate_ids": ["openrouter-free-*", "gemini-free-flash", "codex-spark", "antigravity", "grok-build"]},
        "tool-executor": {"tool_mode": "required", "candidate_ids": ["gemini-free-flash", "openrouter-free-*", "gemini-promo-flash", "codex-spark", "antigravity", "grok-build"]},
        "vision": {"tool_mode": "required", "candidate_ids": ["gemini-free-flash", "gemini-promo-flash"]},
    },
    # These are routing overlays for the canonical Agent Team profiles.  The
    # Team/Subagent graph remains in ``agent_team`` and is never copied here.
    # Provider/model identify the Free Team routing profile; pool_id selects
    # the candidate pool maintained below.
    "llm_profiles": {
        "heavy": {
            "profile_id": "heavy",
            "name": "高負荷",
            "target_type": "pool",
            "provider": "routing-profile",
            "model": "free-team",
            "effort_policy": "same",
            "effort": "",
            "pool_id": "heavy",
            "routing_profile_id": "free-team",
        },
        "light": {
            "profile_id": "light",
            "name": "軽量",
            "target_type": "pool",
            "provider": "routing-profile",
            "model": "free-team",
            "effort_policy": "lower",
            "effort": "",
            "pool_id": "light",
            "routing_profile_id": "free-team",
        },
        "coding": {
            "profile_id": "coding",
            "name": "コーディング",
            "target_type": "pool",
            "provider": "routing-profile",
            "model": "free-team",
            "effort_policy": "same",
            "effort": "",
            "pool_id": "coding",
            "routing_profile_id": "free-team",
        },
        "tool-executor": {
            "profile_id": "tool-executor",
            "name": "ツール実行",
            "target_type": "pool",
            "provider": "routing-profile",
            "model": "free-team",
            "effort_policy": "same",
            "effort": "",
            "pool_id": "tool-executor",
            "routing_profile_id": "free-team",
        },
    },
}


def free_team_profile_template() -> dict[str, Any]:
    """呼び出し側が安全に編集できる初期テンプレートを返す。"""

    return copy.deepcopy(_PROFILE_TEMPLATE)
