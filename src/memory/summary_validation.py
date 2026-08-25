"""Deterministic validation for LLM-generated memory summaries."""

from __future__ import annotations

import re
from dataclasses import dataclass


_FAILURE_OUTPUT_RE = re.compile(
    r"(?i)^\s*(?:null|none|undefined|\[\]|\{\}|error\b|exception\b|"
    r"要約できません|生成できません|エラー(?:が|:))"
)
_ROLE_LINE_RE = re.compile(r"(?m)^(?:ユーザー|アシスタント|user|assistant)\s*:", re.I)


@dataclass(frozen=True)
class SummaryValidation:
    accepted: bool
    reason: str
    normalized: str
    source_chars: int
    previous_chars: int
    result_chars: int


def validate_generated_summary(
    summary: object,
    *,
    source_text: object,
    previous_summary: object = "",
) -> SummaryValidation:
    """Reject malformed or destructively lossy LLM summary candidates.

    The check deliberately uses only deterministic shape and size signals.  It
    does not ask another model to judge a model output and therefore cannot
    create a self-reinforcing memory loop.
    """

    normalized = str(summary or "").strip()
    source = str(source_text or "").strip()
    previous = str(previous_summary or "").strip()
    result_chars = len(normalized)
    source_chars = len(source)
    previous_chars = len(previous)

    def rejected(reason: str) -> SummaryValidation:
        return SummaryValidation(
            accepted=False,
            reason=reason,
            normalized=normalized,
            source_chars=source_chars,
            previous_chars=previous_chars,
            result_chars=result_chars,
        )

    if not normalized:
        return rejected("empty_output")
    if _FAILURE_OUTPUT_RE.search(normalized):
        return rejected("invalid_output")
    if normalized.startswith(("```", "{", "[")) and normalized.endswith(
        ("```", "}", "]")
    ):
        return rejected("structured_output_instead_of_summary")

    # A valid short source may have a short summary.  For larger inputs, require
    # a small but non-trivial result so provider errors and one-line truncation
    # are never promoted to the canonical checkpoint.
    minimum_chars = min(120, max(24, round(source_chars * 0.03)))
    if result_chars < minimum_chars:
        return rejected("abnormally_short")

    # Progressive integration must not silently erase most of the previous
    # canonical summary.  A 45% floor still permits meaningful compaction.
    if previous_chars >= 80 and result_chars < round(previous_chars * 0.45):
        return rejected("previous_summary_mostly_lost")
    if previous and " ".join(normalized.split()) == " ".join(previous.split()):
        return rejected("no_new_content")
    if previous_chars >= 80:
        previous_compact = re.sub(r"\s+", "", previous.casefold())
        result_compact = re.sub(r"\s+", "", normalized.casefold())
        previous_ngrams = {
            previous_compact[index : index + 3]
            for index in range(max(0, len(previous_compact) - 2))
        }
        result_ngrams = {
            result_compact[index : index + 3]
            for index in range(max(0, len(result_compact) - 2))
        }
        if (
            previous_ngrams
            and len(previous_ngrams & result_ngrams) / len(previous_ngrams) < 0.08
        ):
            return rejected("previous_summary_content_lost")

    # If a large source collapses to a generic sentence without any role or
    # topic structure, treat it as provider truncation rather than a summary.
    if source_chars >= 2000 and result_chars < 80 and not _ROLE_LINE_RE.search(
        normalized
    ):
        return rejected("large_source_mostly_lost")

    return SummaryValidation(
        accepted=True,
        reason="accepted",
        normalized=normalized,
        source_chars=source_chars,
        previous_chars=previous_chars,
        result_chars=result_chars,
    )
