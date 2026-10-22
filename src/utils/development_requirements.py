"""Canonical projection for pre-development customer requirements."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_POST_DEVELOPMENT_TASK = re.compile(
    r"(最終レビュー|レビュー|納品|デプロイ|deploy|本番公開|公開|ストア|リリース|顧客確認|受入|検収)",
    re.IGNORECASE,
)


def development_blocking_requirements(metadata: Mapping[str, Any] | None) -> list[str]:
    """Return the server-canonical, bounded blocker projection for Operations UI."""

    metadata = metadata if isinstance(metadata, Mapping) else {}
    if metadata.get("development_requirements_resolved") is True:
        return []
    assessment = metadata.get("specialist_assessment")
    if not isinstance(assessment, Mapping):
        return []
    values: list[str] = []
    for key, prefix in (
        ("development_blockers", "開発開始阻害要因"),
        ("client_inputs_required", "顧客提供物"),
        ("unresolved_requirements", "未確定要件"),
    ):
        raw = assessment.get(key)
        if isinstance(raw, str):
            raw = [raw]
        if isinstance(raw, list):
            values.extend(
                f"{prefix}: {str(item).strip()}"
                for item in raw
                if str(item).strip()
            )
    # Legacy assessments have no development_blockers field. Preserve the
    # backend compatibility rule: clearly post-development human work is not
    # a blocker, while an ambiguous prerequisite fails closed.
    if "development_blockers" not in assessment:
        raw_tasks = assessment.get("human_tasks")
        if isinstance(raw_tasks, str):
            raw_tasks = [raw_tasks]
        if isinstance(raw_tasks, list):
            values.extend(
                f"旧assessmentの要確認人間作業: {str(item).strip()}"
                for item in raw_tasks
                if str(item).strip() and not _POST_DEVELOPMENT_TASK.search(str(item))
            )
    return list(dict.fromkeys(values))[:50]


__all__ = ["development_blocking_requirements"]
