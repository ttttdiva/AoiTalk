"""Shared, dependency-free high-confidence credential patterns.

The display/audit privacy boundaries need to agree on which provider token
shapes are credentials without importing provider clients or the outbound
gateway.  Keep this module intentionally small and dependency-free so it can
also be used by context snapshot sanitization.
"""

from __future__ import annotations

import re


# Provider tokens are deliberately matched only when a known provider prefix
# and a sufficiently long token body are present.  The separator is part of
# each provider's public token format (``-`` for OpenAI/Anthropic/GitLab/
# Slack, ``_`` for GitHub/Hugging Face); accepting both keeps the boundary
# robust to normalized/copy-pasted forms without treating ordinary prose as a
# credential.  The body allows the provider's alphanumeric, underscore, and
# hyphen characters and requires at least twelve characters after the prefix.
HIGH_CONFIDENCE_API_TOKEN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?:"
    r"(?:sk|sk-proj|sk-ant)-[A-Za-z0-9_-]{12,}"
    r"|(?:ghp|github_pat|glpat|hf|xox[abprs])[-_][A-Za-z0-9_-]{12,}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r")(?![A-Za-z0-9_-])"
)


__all__ = ["HIGH_CONFIDENCE_API_TOKEN_RE"]
