"""Cheap, turn-local interpretation hints for AoiTalk product vocabulary."""

from __future__ import annotations

import re
from dataclasses import dataclass


_INBOX_RE = re.compile(r"(?<![a-z0-9_])inbox(?![a-z0-9_])", re.IGNORECASE)
_DOCS_RE = re.compile(r"(?<![a-z])docs(?![a-z])", re.IGNORECASE)
_WORKSPACE_RE = re.compile(
    r"ワークスペース|(?<![a-z])workspaces?(?![a-z])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AoiVocabularySignals:
    inbox: bool = False
    project_information: bool = False
    project_workspace: bool = False
    docs: bool = False

    @property
    def any(self) -> bool:
        return any(
            (
                self.inbox,
                self.project_information,
                self.project_workspace,
                self.docs,
            )
        )


def detect_aoi_vocabulary(text: str) -> AoiVocabularySignals:
    """Detect only reserved product words; no DB, network, or LLM work."""

    value = str(text or "")
    stripped_lines = {
        line.strip().casefold() for line in value.splitlines() if line.strip()
    }
    is_inbox_command = "/inbox" in stripped_lines
    return AoiVocabularySignals(
        inbox=not is_inbox_command and _INBOX_RE.search(value) is not None,
        project_information="案件情報" in value,
        project_workspace=_WORKSPACE_RE.search(value) is not None,
        docs=_DOCS_RE.search(value) is not None,
    )


def build_aoi_vocabulary_hint(
    text: str,
    *,
    inbox_search_available: bool,
) -> str:
    """Build only the lines needed for concepts mentioned in this turn."""

    signals = detect_aoi_vocabulary(text)
    if not signals.any:
        return ""

    lines: list[str] = []
    if signals.inbox:
        # Vocabulary is semantic context only.  Tool names and imperative
        # routing belong to explicit command capabilities/LLM selection, not
        # to ordinary keyword-triggered hints.
        lines.append(
            "- `inbox` は通常、実案件の `案件情報 / <案件> / Inbox` にある"
            "Work Intake項目です。既定Inbox Space/Projectとは区別してください。"
        )
    if signals.project_information:
        lines.append(
            "- `案件情報` は、対象Projectに紐づくDocs正本"
            "（Project.knowledge_node_id）とその配下を指します。"
        )
    if signals.project_workspace:
        lines.append(
            "- `workspace` / `ワークスペース` は、対象Projectに紐づく"
            "ファイラータブのディレクトリを指します。Docsとは別です。"
        )
    if signals.docs:
        lines.append(
            "- `Docs` はDocsタブのKnowledgeNodeを指します。"
            "ファイラーのworkspaceや過去チャットとは別です。"
        )
    return "\n".join(["## AoiTalk Vocabulary", *lines])
