"""プロバイダ識別子の正規化。

DB に実在する生文字列（`claude-code` / `grok-build-cli` など）を、
料金カタログで使う正規 provider ID へ寄せる責務だけを持つ。
"""

from __future__ import annotations

import re
from typing import Dict, FrozenSet, Optional

__all__ = [
    "CLI_PROVIDERS",
    "LOCAL_PROVIDERS",
    "PROVIDER_REPORTED_PROVIDERS",
    "PROVIDER_ALIASES",
    "UNKNOWN_PROVIDER",
    "canonical_provider",
    "is_cli_provider",
    "is_local_provider",
    "is_provider_reported_provider",
]

UNKNOWN_PROVIDER = "unknown"

# サブスクリプション / クォータ制で従量課金しない CLI 系プロバイダ
CLI_PROVIDERS: FrozenSet[str] = frozenset(
    {"codex-cli", "claude-cli", "antigravity-cli", "grok-cli"}
)

# ローカル実行（API 従量課金なし）
LOCAL_PROVIDERS: FrozenSet[str] = frozenset(
    {"ollama", "sglang", "openai_compatible_local"}
)

# プロバイダ自身が実費を返してくるもの
PROVIDER_REPORTED_PROVIDERS: FrozenSet[str] = frozenset({"openrouter"})

# DB に実在する生文字列 → 正規 provider ID
PROVIDER_ALIASES: Dict[str, str] = {
    "claude-code": "claude-cli",
    "claude code": "claude-cli",
    "grok-build-cli": "grok-cli",
    "grok build cli": "grok-cli",
    "codex cli": "codex-cli",
    "antigravity cli": "antigravity-cli",
    "google": "gemini",
    "google-gemini": "gemini",
    "moonshot": "kimi",
    "openai-compatible-local": "openai_compatible_local",
}

_WHITESPACE_RE = re.compile(r"\s+")


def canonical_provider(raw: Optional[str]) -> str:
    """小文字化・trim・空白→ハイフン正規化のうえ PROVIDER_ALIASES を適用する。

    None や空文字は ``"unknown"`` を返す。
    """
    if raw is None:
        return UNKNOWN_PROVIDER
    value = str(raw).strip().lower()
    if not value:
        return UNKNOWN_PROVIDER
    # 空白を含む生文字列も alias 表で直接引けるようにする
    if value in PROVIDER_ALIASES:
        return PROVIDER_ALIASES[value]
    collapsed = _WHITESPACE_RE.sub("-", value)
    if collapsed in PROVIDER_ALIASES:
        return PROVIDER_ALIASES[collapsed]
    return collapsed


def is_cli_provider(provider: Optional[str]) -> bool:
    """CLI（サブスクリプション）系プロバイダなら True。"""
    return canonical_provider(provider) in CLI_PROVIDERS


def is_local_provider(provider: Optional[str]) -> bool:
    """ローカル実行系プロバイダなら True。"""
    return canonical_provider(provider) in LOCAL_PROVIDERS


def is_provider_reported_provider(provider: Optional[str]) -> bool:
    """プロバイダ自身が実費を返す系なら True。"""
    return canonical_provider(provider) in PROVIDER_REPORTED_PROVIDERS
